"""Exercise the packaged MCP stdio endpoint without a GUI or user settings."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any


def smoke_test(executable: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="rimsort-mcp-smoke-") as directory:
        root = Path(directory)
        config = root / "config"
        config.mkdir()
        settings = root / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "current_instance": "Default",
                    "instances": {
                        "Default": {"name": "Default", "config_folder": str(config)}
                    },
                }
            ),
            encoding="utf-8",
        )
        environment = dict(os.environ, RIMSORT_MCP_SETTINGS=str(settings))
        responses: queue.Queue[str] = queue.Queue()
        with (root / "stderr.log").open("w+") as errors:
            process = subprocess.Popen(
                [str(executable.resolve()), "mcp", "--headless"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                encoding="utf-8",
                env=environment,
            )
            assert process.stdin is not None and process.stdout is not None

            def read_stdout() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    responses.put(line)
                responses.put("")

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()

            def send(payload: dict[str, object]) -> None:
                assert process.stdin is not None
                process.stdin.write(json.dumps(payload) + "\n")
                process.stdin.flush()

            def receive(request_id: int) -> dict[str, Any]:
                for _ in range(100):
                    line = responses.get(timeout=30)
                    if not line:
                        raise RuntimeError("MCP executable exited before responding")
                    response = json.loads(line)
                    if response.get("id") == request_id:
                        if "error" in response:
                            raise RuntimeError(str(response["error"]))
                        return response["result"]
                raise RuntimeError("Too many unexpected MCP messages")

            try:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "release-smoke", "version": "1"},
                        },
                    }
                )
                receive(1)
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                tools = receive(2)["tools"]
                names = {tool["name"] for tool in tools}
                required = {
                    "get_status",
                    "get_modlist_state",
                    "apply_modlist_plan",
                    "gui_navigate",
                    "get_gui_state",
                }
                if not required <= names:
                    raise RuntimeError(
                        f"Packaged MCP tools missing: {required - names}"
                    )
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "get_gui_state", "arguments": {}},
                    }
                )
                result = receive(3)
                if "gui_unavailable" not in json.dumps(result):
                    raise RuntimeError("Headless GUI state was not reported correctly")
                print(
                    f"MCP binary smoke passed: {len(names)} tools, initialize/list/call OK"
                )
            except Exception:
                errors.flush()
                errors.seek(0)
                print(errors.read()[-10000:], file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                reader.join(timeout=2)


if __name__ == "__main__":
    smoke_test(Path(sys.argv[1]))
