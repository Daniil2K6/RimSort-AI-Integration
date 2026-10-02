from collections.abc import Mapping, Sequence

import networkx as nx
from loguru import logger
from PySide6.QtCore import QCoreApplication
from toposort import CircularDependencyError, toposort

from app.models.metadata.metadata_structure import AboutXmlMod, ListedMod
from app.views.dialogue import show_warning


def order_topo_levels(
    levels: Sequence[set[str]],
    active_mod_paths: set[str],
    mods_metadata: Mapping[str, ListedMod],
) -> list[str]:
    """Order topological levels into a flat path list.

    Within each level, mods are sorted alphabetically by name for
    consistency. Levels themselves are already dependency-ordered by
    the caller.

    :param levels: Topologically-ordered dependency levels (package ids).
    :param active_mod_paths: Set of mod paths (identifiers) to sort
    :param mods_metadata: path -> ListedMod mapping for name lookups
    :return: Sorted list of mod paths
    """
    packageid_to_path: dict[str, str] = {}
    path_to_name: dict[str, str] = {}

    for path in active_mod_paths:
        mod = mods_metadata.get(path)
        if not isinstance(mod, AboutXmlMod):
            logger.warning(f"Missing or non-AboutXmlMod for path {path}, skipping")
            continue
        pid = str(mod.package_id)
        packageid_to_path[pid] = path
        name = mod.name if isinstance(mod.name, str) else "name error in mod about.xml"
        path_to_name[path] = name

    def safe_name(name: object) -> str:
        if isinstance(name, str):
            return name.lower()
        return "name error in mod about.xml"

    reordered: list[str] = []
    for level in levels:
        temp_mod_list = [
            packageid_to_path[package_id]
            for package_id in level
            if package_id in packageid_to_path
        ]
        sorted_temp = sorted(
            temp_mod_list,
            key=lambda p: safe_name(path_to_name.get(p)),
            reverse=False,
        )
        reordered.extend(sorted_temp)

    return reordered


def do_topo_sort(
    dependency_graph: dict[str, set[str]],
    active_mod_paths: set[str],
    mods_metadata: Mapping[str, ListedMod],
) -> list[str]:
    """Sort mods using the topological sort algorithm.

    For each topological level, sort the mods alphabetically by name
    for consistency.

    :param dependency_graph: package_id -> set of dependency package_ids
    :param active_mod_paths: Set of mod paths (identifiers) to sort
    :param mods_metadata: path -> ListedMod mapping for name lookups
    :return: Sorted list of mod paths
    """
    logger.info(f"Initializing toposort for {len(dependency_graph)} mods")

    try:
        sorted_dependencies = list(toposort(dependency_graph))
    except CircularDependencyError as e:
        find_circular_dependencies(dependency_graph)
        raise e

    reordered = order_topo_levels(sorted_dependencies, active_mod_paths, mods_metadata)

    logger.info(f"Finished Toposort sort with {len(reordered)} mods")
    return reordered


def describe_circular_dependencies(
    dependency_graph: dict[str, set[str]],
) -> list[str]:
    """Return human-readable strings for every dependency cycle found.

    Pure helper with no UI side effects; callers decide how to surface
    the result (GUI shows a dialog, headless code raises/serializes).

    :param dependency_graph: package_id -> set of dependency package_ids
    :return: List of cycle strings such as ``"a -> b -> a"``
    """
    graph = nx.DiGraph(dependency_graph)  # type: ignore
    cycles = list(nx.simple_cycles(graph))

    cycle_strings = []
    if cycles:
        logger.info("Circular dependencies detected:")
        for cycle in cycles:
            loop = " -> ".join(cycle)
            logger.info(loop)
            cycle_strings.append(loop)
    else:
        logger.info("No circular dependencies found.")

    return cycle_strings


def find_circular_dependencies(dependency_graph: dict[str, set[str]]) -> None:
    cycle_strings = describe_circular_dependencies(dependency_graph)

    show_warning(
        title=QCoreApplication.translate(
            "find_circular_dependencies", "Unable to Sort"
        ),
        text=QCoreApplication.translate("find_circular_dependencies", "Unable to Sort"),
        information=QCoreApplication.translate(
            "find_circular_dependencies",
            "RimSort found circular dependencies in your mods list. Please see the details for dependency loops.",
        ),
        details="\n\n".join(cycle_strings),
    )
