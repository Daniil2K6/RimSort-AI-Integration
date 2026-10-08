"""Workshop update checks and SteamCMD downloads for the MCP server.

Everything here must run headless: no Qt dialogs, no event loop. The
Steam WebAPI is queried directly and SteamCMD is driven through
``subprocess`` with console-output parsing (mirroring RunnerPanel's
success/failure detection).
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from loguru import logger
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.context import MCPContext
from app.models.metadata.metadata_structure import AboutXmlMod, ModType
from app.utils import http, symlink
from app.utils.steam.steamcmd.wrapper import STEAMCMD_BATCH_SIZE
from app.utils.steam.steamfiles.wrapper import acf_to_dict
from app.utils.steam.webapi.wrapper import ISteamRemoteStorage_GetPublishedFileDetails

_WORKSHOP_SOURCES = (ModType.STEAM_WORKSHOP, ModType.STEAM_CMD)
_SUCCESS_RE = re.compile(r"Success\. Downloaded item (\d+)")
_FAILURE_RE = re.compile(r"ERROR! Download item (\d+)")
_NOT_LOGGED_ON = "ERROR! Not logged on."
_REPORT_CAP = 200
_DEFAULT_BATCH_TIMEOUT = 1800
_STEAMCMD_APP_ID = "294100"
_SKIPPED_DIR_NAMES = frozenset({".git", ".vs", "__pycache__", ".svn"})
#: Steam account password for headless logins (never stored by RimSort).
STEAM_PASSWORD_ENV = "RIMSORT_STEAM_PASSWORD"
#: Optional Steam Guard code for the first headless login.
STEAM_GUARD_ENV = "RIMSORT_STEAM_GUARD_CODE"
_LOGIN_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _iso(timestamp: float | None) -> str | None:
    if not timestamp or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(int(timestamp), UTC).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return None


def _parse_sources(sources: str) -> set[str]:
    """Parse a comma-separated source filter ("", "all" -> every workshop type)."""
    raw = sources.strip()
    if not raw or raw.lower() == "all":
        return {member.value for member in _WORKSHOP_SOURCES}
    valid = {member.value.lower(): member.value for member in _WORKSHOP_SOURCES}
    picked: set[str] = set()
    for part in raw.split(","):
        key = part.strip().lower()
        if not key:
            continue
        if key not in valid:
            raise ToolError(
                f"Unknown source {part.strip()!r}. "
                f"Use {', '.join(sorted(member.value for member in _WORKSHOP_SOURCES))}"
            )
        picked.add(valid[key])
    if not picked:
        raise ToolError("sources filter resolved to an empty set")
    return picked


def _workshop_candidates(
    ctx: MCPContext, source_filter: set[str]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path, mod in ctx.mods_metadata.items():
        if not isinstance(mod, AboutXmlMod):
            continue
        if mod.mod_type not in _WORKSHOP_SOURCES:
            continue
        if mod.mod_type.value not in source_filter:
            continue
        pfid = str(mod.published_file_id or "").strip()
        if not pfid.isdigit():
            continue
        entries.append(
            {
                "id": str(mod.package_id),
                "name": mod.name if isinstance(mod.name, str) else "",
                "pfid": pfid,
                "source": mod.mod_type.value,
                "path": path,
            }
        )
    entries.sort(key=lambda entry: (entry["name"].lower(), entry["id"]))
    return entries


def _acf_time_map(acf_path: Path) -> dict[str, int]:
    """pfid -> WorkshopItemsInstalled/Details timeupdated from one ACF file."""
    if not acf_path.is_file():
        return {}
    try:
        data = acf_to_dict(str(acf_path))
    except Exception as exc:  # one bad ACF must not kill the update check
        logger.warning(f"Unable to read ACF {acf_path}: {exc}")
        return {}
    details = (data.get("AppWorkshop") or {}).get("WorkshopItemDetails") or {}
    if not isinstance(details, dict):
        return {}
    times: dict[str, int] = {}
    for pfid, entry in details.items():
        if not isinstance(entry, dict):
            continue
        try:
            value = int(entry.get("timeupdated", 0))
        except (TypeError, ValueError):
            continue
        if value > 0:
            times[str(pfid)] = value
    return times


def _installed_time_maps(ctx: MCPContext) -> dict[str, int]:
    """pfid -> installed-content update time from every available ACF."""
    merged: dict[str, int] = {}
    sources: list[Path] = []
    prefix = str(ctx.instance.steamcmd_install_path or "")
    if prefix:
        sources.append(
            Path(prefix)
            / "steam"
            / "steamapps"
            / "workshop"
            / f"appworkshop_{_STEAMCMD_APP_ID}.acf"
        )
    workshop = ctx.workshop_folder
    if workshop is not None:
        sources.append(workshop.parent.parent / f"appworkshop_{_STEAMCMD_APP_ID}.acf")
    for acf_path in sources:
        for pfid, value in _acf_time_map(acf_path).items():
            merged[pfid] = max(merged.get(pfid, 0), value)
    return merged


def _newest_file_mtime(path: Path) -> float:
    """Newest mtime under ``path`` (skips VCS dirs); fallback when no ACF."""
    try:
        newest = path.stat().st_mtime
    except OSError:
        return 0.0
    for root, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if name not in _SKIPPED_DIR_NAMES]
        for name in files:
            try:
                mtime = os.stat(os.path.join(root, name)).st_mtime
            except OSError:
                continue
            newest = max(newest, mtime)
    return newest


def _query_remote_times(
    pfids: list[str],
) -> tuple[dict[str, dict[str, Any]], list[str], list[str], list[str]]:
    """Steam WebAPI lookup. Returns (remote, unavailable, failed, errors)."""
    metadata_list, failed_pfids, errors = ISteamRemoteStorage_GetPublishedFileDetails(
        pfids
    )
    remote: dict[str, dict[str, Any]] = {}
    unavailable: list[str] = []
    for entry in metadata_list:
        pfid = str(entry.get("publishedfileid", "")).strip()
        if not pfid:
            continue
        try:
            result = int(entry.get("result", 0))
        except (TypeError, ValueError):
            result = 0
        try:
            time_updated = int(entry.get("time_updated", 0))
        except (TypeError, ValueError):
            time_updated = 0
        if result == 1 and time_updated > 0:
            remote[pfid] = {
                "time_updated": time_updated,
                "title": str(entry.get("title") or ""),
            }
        else:
            unavailable.append(pfid)
    return remote, unavailable, failed_pfids, errors


def _run_check(ctx: MCPContext, source_filter: set[str]) -> dict[str, Any]:
    """Full (uncapped) update-check result used by both tools."""
    entries = _workshop_candidates(ctx, source_filter)
    if not entries:
        return {
            "status": "no_workshop_mods",
            "mods_checked": 0,
            "outdated": [],
            "outdated_count": 0,
            "unavailable": [],
            "failed_pfids": [],
            "errors": [],
        }

    remote, unavailable, failed_pfids, errors = _query_remote_times(
        [entry["pfid"] for entry in entries]
    )
    installed_times = _installed_time_maps(ctx)

    outdated: list[dict[str, Any]] = []
    checked = 0
    for entry in entries:
        remote_info = remote.get(entry["pfid"])
        if remote_info is None:
            continue
        checked += 1
        installed = installed_times.get(entry["pfid"], 0)
        if not installed:
            installed = int(_newest_file_mtime(Path(entry["path"])))
        if remote_info["time_updated"] > installed:
            outdated.append(
                {
                    "id": entry["id"],
                    "name": entry["name"],
                    "pfid": entry["pfid"],
                    "source": entry["source"],
                    "installed_time": _iso(installed),
                    "workshop_time": _iso(remote_info["time_updated"]),
                    "installed_ts": installed,
                    "workshop_ts": remote_info["time_updated"],
                }
            )

    if failed_pfids and checked:
        status = "partial"
    elif failed_pfids:
        status = "failed"
    else:
        status = "success"
    return {
        "status": status,
        "mods_checked": checked,
        "outdated": outdated,
        "outdated_count": len(outdated),
        "unavailable": unavailable,
        "failed_pfids": failed_pfids,
        "errors": errors,
    }


def check_workshop_updates(
    ctx: MCPContext, sources: str = "", cap: int = _REPORT_CAP
) -> dict[str, Any]:
    """Compare installed Workshop/SteamCMD mods against the Steam WebAPI.

    :param sources: comma-separated filter ("", ``"all"`` = both types).
    :param cap: maximum outdated entries to include in the payload.
    """
    source_filter = _parse_sources(sources)
    result = _run_check(ctx, source_filter)
    outdated: list[dict[str, Any]] = result["outdated"]
    result["outdated"] = outdated[:cap]
    result["truncated"] = len(outdated) > cap
    result["sources"] = sorted(source_filter)
    result["failed_pfids"] = result["failed_pfids"][:cap]
    result["unavailable"] = result["unavailable"][:cap]
    return result


# ---- SteamCMD downloads ----


def _steamcmd_executable(prefix: Path) -> Path:
    name = "steamcmd.exe" if platform.system() == "Windows" else "steamcmd.sh"
    return prefix / "steamcmd" / name


def _steamcmd_download_url() -> str:
    system = platform.system()
    if system == "Darwin":
        return "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_osx.tar.gz"
    if system == "Linux":
        return "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
    if system == "Windows":
        return "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
    raise ToolError(
        f"SteamCMD is not supported on platform {system!r}; "
        "set it up manually and re-run without install_steamcmd."
    )


def _install_steamcmd(prefix: Path) -> None:
    """Download and unpack SteamCMD into ``prefix/steamcmd`` (headless)."""
    url = _steamcmd_download_url()
    install_path = prefix / "steamcmd"
    install_path.mkdir(parents=True, exist_ok=True)
    logger.info(f"Downloading SteamCMD from {url} into {install_path}")
    try:
        if url.endswith(".zip"):
            from zipfile import ZipFile

            with ZipFile(BytesIO(http.get(url, timeout=120).content)) as archive:
                archive.extractall(install_path)
        else:
            import tarfile

            payload = http.get(url, timeout=120, stream=True).content
            with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as archive:
                archive.extractall(install_path, filter="data")
    except Exception as exc:
        raise ToolError(f"Failed to download/unpack SteamCMD: {exc}") from exc
    executable = _steamcmd_executable(prefix)
    if executable.is_file() and platform.system() != "Windows":
        executable.chmod(executable.stat().st_mode | 0o755)
    if not executable.is_file():
        raise ToolError(f"SteamCMD archive extracted but {executable} is still missing")


def _steamcmd_env(prefix: Path) -> dict[str, str] | None:
    """Isolated HOME for SteamCMD on Linux (mirrors SteamcmdInterface)."""
    if platform.system() != "Linux":
        return None
    home = prefix / "home"
    config = home / ".config"
    data = home / ".local" / "share"
    config.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(data),
    }


def _ensure_workshop_symlink(ctx: MCPContext, prefix: Path) -> str:
    """Point SteamCMD's workshop content dir at the instance's local mods.

    SteamCMD downloads into ``<prefix>/steam/steamapps/workshop/content/294100``;
    RimSort symlinks that path at the local mods folder so downloaded mods
    become visible to the scanner (see InstanceService).
    """
    if ctx.local_folder is None:
        raise ToolError("Instance has no local mods folder configured")
    link_path = (
        prefix / "steam" / "steamapps" / "workshop" / "content" / str(_STEAMCMD_APP_ID)
    )
    if link_path.is_symlink():
        return f"symlink already present: {link_path}"
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        symlink.create_symlink(str(ctx.local_folder), str(link_path))
    except symlink.SymlinkDstNotEmptyError:
        return (
            f"warning: {link_path} is a real non-empty directory (not a symlink); "
            "SteamCMD would download into it and RimSort may not see the mods"
        )
    except symlink.SymlinkCreationError as exc:
        raise ToolError(f"Could not create steamcmd symlink: {exc}") from exc
    return f"created symlink: {link_path} -> {ctx.local_folder}"


# jscpd:ignore-start
def _validate_login(login: str) -> str:
    """Sanitize a Steam account name (or ``anonymous``) for the runscript."""
    name = (login or "anonymous").strip()
    if not _LOGIN_RE.fullmatch(name):
        raise ToolError(
            "login must be 'anonymous' or a Steam account name "
            "(letters, digits, '_', '-', '.')"
        )
    return name


def _login_lines(login: str) -> list[str]:
    """Runscript lines logging SteamCMD in as ``login``.

    The password is read from :data:`STEAM_PASSWORD_ENV` (headless runs)
    and the Steam Guard code from :data:`STEAM_GUARD_ENV`; neither is ever
    stored in settings.json or written to logs. Newlines are rejected so
    no value can inject extra runscript commands.
    """
    if login == "anonymous":
        return ["login anonymous"]
    lines: list[str] = []
    guard = os.environ.get(STEAM_GUARD_ENV, "").strip()
    if guard:
        if "\n" in guard or "\r" in guard:
            raise ToolError(f"{STEAM_GUARD_ENV} must not contain newlines")
        lines.append(f"set_steam_guard_code {guard}")
    password = os.environ.get(STEAM_PASSWORD_ENV, "")
    if "\n" in password or "\r" in password:
        raise ToolError(f"{STEAM_PASSWORD_ENV} must not contain newlines")
    if password:
        lines.append(f"login {login} {password}")
    else:
        lines.append(f"login {login}")
    return lines


def _build_download_script(
    prefix: Path, pfids: list[str], validate: bool, login: str = "anonymous"
) -> str:
    """Write a (0600) SteamCMD runscript for ``pfids`` and return its path."""
    download_cmd = f"workshop_download_item {_STEAMCMD_APP_ID}"
    lines = [f'force_install_dir "{prefix / "steam"}"', *_login_lines(login)]
    for pfid in pfids:
        lines.append(f"{download_cmd} {pfid}" + (" validate" if validate else ""))
    lines.append("quit\n")

    script_path = Path(gettempdir()) / "rimsort_mcp_steamcmd_script.txt"
    script_path.write_text("\n".join(lines), encoding="utf-8")
    script_path.chmod(0o600)
    return str(script_path)


# jscpd:ignore-end


def _parse_batch_output(
    batch: list[str], output: str
) -> tuple[list[str], dict[str, str], bool]:
    """Split SteamCMD output into (downloaded, failed, login_error)."""
    downloaded = set(_SUCCESS_RE.findall(output))
    failed = dict.fromkeys(_FAILURE_RE.findall(output), "SteamCMD reported an error")
    login_error = _NOT_LOGGED_ON in output
    done: list[str] = []
    problems: dict[str, str] = {}
    unreported: list[str] = []
    for pfid in batch:
        if pfid in downloaded:
            done.append(pfid)
        elif pfid in failed:
            problems[pfid] = failed[pfid]
        else:
            unreported.append(pfid)
    if unreported:
        for pfid in unreported:
            problems[pfid] = "no result reported by SteamCMD"
    return done, problems, login_error


def update_mods(
    ctx: MCPContext,
    package_ids: list[str] | None = None,
    publishedfileids: list[str] | None = None,
    outdated_only: bool = False,
    sources: str = "Steam CMD",
    validate: bool | None = None,
    install_steamcmd: bool = False,
    login: str = "anonymous",
    dry_run: bool = False,
    batch_timeout: int = _DEFAULT_BATCH_TIMEOUT,
) -> dict[str, Any]:
    """Download/update Workshop mods through SteamCMD (headless, blocking)."""
    source_filter = _parse_sources(sources)
    login = _validate_login(login)

    targets: dict[str, str] = {}
    if package_ids:
        for pid in package_ids:
            paths = ctx.paths_for_pid(pid)
            if not paths:
                raise ToolError(
                    f"No installed mod with packageId {pid!r}. Use list_mods to search."
                )
            resolved = False
            for path in paths:
                mod = ctx.mods_metadata.get(path)
                if isinstance(mod, AboutXmlMod):
                    pfid = str(mod.published_file_id or "").strip()
                    if pfid.isdigit():
                        targets[pfid] = pid
                        resolved = True
            if not resolved:
                raise ToolError(
                    f"{pid!r} has no PublishedFileId; only Workshop/SteamCMD "
                    "mods can be updated this way"
                )
    for pfid in publishedfileids or []:
        clean = str(pfid).strip()
        if not clean.isdigit():
            raise ToolError(f"Invalid publishedfileid {pfid!r} (digits only)")
        targets.setdefault(clean, "explicit")

    if outdated_only:
        check = _run_check(ctx, source_filter)
        for entry in check["outdated"]:
            targets.setdefault(entry["pfid"], entry["id"])

    if not targets:
        raise ToolError(
            "Nothing to update: provide package_ids, publishedfileids or "
            "outdated_only=true"
        )

    ordered = sorted(targets)
    batch_count = (len(ordered) + STEAMCMD_BATCH_SIZE - 1) // STEAMCMD_BATCH_SIZE
    if dry_run:
        return {
            "dry_run": True,
            "count": len(ordered),
            "publishedfileids": ordered,
            "targets": sorted({targets[pfid] for pfid in ordered}),
            "batches": batch_count,
            "sources": sorted(source_filter),
            "login": login,
        }

    prefix_raw = str(ctx.instance.steamcmd_install_path or "")
    if not prefix_raw:
        raise ToolError("Instance has no steamcmd_install_path configured")
    prefix = Path(prefix_raw)
    executable = _steamcmd_executable(prefix)
    installed_now = False
    if not executable.is_file():
        if not install_steamcmd:
            raise ToolError(
                f"SteamCMD not found at {executable}. Re-run with "
                "install_steamcmd=true to download it (requires internet), or "
                "set it up from RimSort (Settings -> SteamCMD)."
            )
        _install_steamcmd(prefix)
        installed_now = True

    symlink_note = _ensure_workshop_symlink(ctx, prefix)

    depot_cache_cleared = False
    if ctx.instance.steamcmd_auto_clear_depot_cache:
        depot = prefix / "steamcmd" / "depotcache"
        if depot.is_dir():
            shutil.rmtree(depot, ignore_errors=True)
            depot_cache_cleared = True

    if validate is None:
        validate = bool(ctx.load_settings().get("steamcmd_validate_downloads", True))

    env = _steamcmd_env(prefix)
    downloaded: list[str] = []
    failed: dict[str, str] = {}
    login_error = False
    script: str | None = None
    try:
        for index in range(batch_count):
            batch = ordered[
                index * STEAMCMD_BATCH_SIZE : (index + 1) * STEAMCMD_BATCH_SIZE
            ]
            script = _build_download_script(prefix, batch, validate, login)
            logger.info(
                f"MCP steamcmd batch {index + 1}/{batch_count}: {len(batch)} mod(s)"
            )
            try:
                proc = subprocess.run(
                    [str(executable), "+runscript", script],
                    capture_output=True,
                    text=True,
                    timeout=batch_timeout,
                    env=env,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                for pfid in batch:
                    failed[pfid] = f"batch timed out after {batch_timeout}s"
                continue
            except OSError as exc:
                raise ToolError(f"Failed to launch SteamCMD: {exc}") from exc
            output = (proc.stdout or "") + "\n" + (proc.stderr or "")
            done, problems, not_logged_on = _parse_batch_output(batch, output)
            downloaded.extend(done)
            failed.update(problems)
            login_error = login_error or not_logged_on
    finally:
        # The runscript may contain credentials — always remove it.
        if script is not None:
            Path(script).unlink(missing_ok=True)

    scan = ctx.refresh()
    failed_capped = dict(list(failed.items())[:_REPORT_CAP])
    return {
        "count": len(ordered),
        "downloaded_count": len(downloaded),
        "downloaded": downloaded[:_REPORT_CAP],
        "failed_count": len(failed),
        "failed": failed_capped,
        "batches": batch_count,
        "validate": validate,
        "login": login,
        "steamcmd_installed_now": installed_now,
        "symlink": symlink_note,
        "depot_cache_cleared": depot_cache_cleared,
        "login_error": login_error,
        "scan": {"mods_found": scan, "instance": ctx.instance_name},
    }
