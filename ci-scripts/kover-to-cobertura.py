from __future__ import annotations

import argparse
import os
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


Counter = Tuple[int, int]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a Kover XML report to Cobertura XML for GitHub Code Quality.",
    )
    parser.add_argument("input", help="Path to the Kover XML report")
    parser.add_argument("output", help="Path to write the Cobertura XML report")
    parser.add_argument(
        "--source-root",
        action="append",
        default=[],
        dest="source_roots",
        metavar="DIR",
        help=(
            "Source directory recorded in Cobertura <sources> "
            "(repeatable). Defaults to '.' if omitted."
        ),
    )
    return parser.parse_args(argv)


def counter_values(element: ET.Element, counter_type: str) -> Counter:
    for child in element.findall("counter"):
        if child.get("type") == counter_type:
            return int(child.get("covered", "0")), int(child.get("missed", "0"))
    return 0, 0


def rate(covered: int, missed: int) -> float:
    total = covered + missed
    if total == 0:
        return 0.0
    return covered / total


def fmt_rate(covered: int, missed: int) -> str:
    return f"{rate(covered, missed):.4f}"


def set_rates(target: ET.Element, source: ET.Element) -> Counter:
    """Copy LINE/BRANCH rates from a Kover element onto a Cobertura element."""
    line_covered, line_missed = counter_values(source, "LINE")
    branch_covered, branch_missed = counter_values(source, "BRANCH")
    target.set("line-rate", fmt_rate(line_covered, line_missed))
    target.set("branch-rate", fmt_rate(branch_covered, branch_missed))
    target.set("complexity", "0")
    return line_covered, line_missed


def set_coverage_totals(target: ET.Element, source: ET.Element) -> None:
    line_covered, line_missed = counter_values(source, "LINE")
    branch_covered, branch_missed = counter_values(source, "BRANCH")
    target.set("line-rate", fmt_rate(line_covered, line_missed))
    target.set("branch-rate", fmt_rate(branch_covered, branch_missed))
    target.set("lines-covered", str(line_covered))
    target.set("lines-valid", str(line_covered + line_missed))
    target.set("branches-covered", str(branch_covered))
    target.set("branches-valid", str(branch_covered + branch_missed))
    target.set("complexity", "0")
    target.set("version", "1.0")


def timestamp_seconds(report: ET.Element) -> str:
    session = report.find("sessioninfo")
    if session is not None and session.get("start"):
        try:
            return str(int(int(session.get("start")) / 1000))
        except (TypeError, ValueError):
            pass
    return str(int(time.time()))


def convert_lines(kover_lines: Iterable[ET.Element], parent: ET.Element) -> None:
    lines_el = ET.SubElement(parent, "lines")
    for kline in kover_lines:
        ci = int(kline.get("ci", "0"))
        mb = int(kline.get("mb", "0"))
        cb = int(kline.get("cb", "0"))

        cline = ET.SubElement(lines_el, "line")
        cline.set("number", kline.get("nr", "0"))
        cline.set("hits", str(ci) if ci > 0 else "0")

        branch_total = mb + cb
        if branch_total > 0:
            pct = int(100 * (cb / branch_total))
            cline.set("branch", "true")
            cline.set(
                "condition-coverage",
                f"{pct}% ({cb}/{branch_total})",
            )
            conditions = ET.SubElement(cline, "conditions")
            condition = ET.SubElement(conditions, "condition")
            condition.set("number", "0")
            condition.set("type", "jump")
            condition.set("coverage", f"{pct}%")
        else:
            cline.set("branch", "false")


def convert_method(kover_method: ET.Element) -> ET.Element:
    method = ET.Element("method")
    method.set("name", kover_method.get("name", ""))
    method.set("signature", kover_method.get("desc", ""))
    set_rates(method, kover_method)
    ET.SubElement(method, "lines")
    return method


def source_filename_for_class(kover_class: ET.Element) -> str:
    explicit = kover_class.get("sourcefilename")
    if explicit:
        return explicit
    class_path = kover_class.get("name", "")
    top_level = class_path.split("$", 1)[0]
    return os.path.basename(top_level) + ".java"


def file_class_name(package_name: str, source_filename: str, classes: List[ET.Element]) -> str:
    stem = source_filename.rsplit(".", 1)[0]
    expected = f"{package_name}/{stem}"
    for kclass in classes:
        if kclass.get("name") == expected:
            return expected.replace("/", ".")
    for kclass in classes:
        name = kclass.get("name", "")
        if "$" not in os.path.basename(name):
            return name.replace("/", ".")
    return expected.replace("/", ".")


def convert_source_file(
    package_name: str,
    source_filename: str,
    kover_classes: List[ET.Element],
    kover_sourcefile: Optional[ET.Element],
) -> ET.Element:
    c_class = ET.Element("class")
    c_class.set("name", file_class_name(package_name, source_filename, kover_classes))
    c_class.set("filename", f"{package_name}/{source_filename}")

    methods_el = ET.SubElement(c_class, "methods")
    for kclass in kover_classes:
        for kmethod in kclass.findall("method"):
            methods_el.append(convert_method(kmethod))

    rate_source = kover_sourcefile if kover_sourcefile is not None else kover_classes[0]
    set_rates(c_class, rate_source)

    kover_lines = (
        list(kover_sourcefile.findall("line")) if kover_sourcefile is not None else []
    )
    convert_lines(kover_lines, c_class)
    return c_class


def convert_package(kover_package: ET.Element) -> ET.Element:
    package_name = kover_package.get("name", "")
    c_package = ET.Element("package")
    c_package.set("name", package_name.replace("/", "."))

    sourcefiles = {
        sf.get("name"): sf for sf in kover_package.findall("sourcefile") if sf.get("name")
    }
    classes_by_file: Dict[str, List[ET.Element]] = defaultdict(list)
    for kclass in kover_package.findall("class"):
        classes_by_file[source_filename_for_class(kclass)].append(kclass)

    for filename in sourcefiles:
        classes_by_file.setdefault(filename, [])

    classes_el = ET.SubElement(c_package, "classes")
    for source_filename, kover_classes in sorted(classes_by_file.items()):
        if not kover_classes and source_filename not in sourcefiles:
            continue
        classes_el.append(
            convert_source_file(
                package_name,
                source_filename,
                kover_classes,
                sourcefiles.get(source_filename),
            )
        )

    set_rates(c_package, kover_package)
    return c_package


def convert_report(report: ET.Element, source_roots: Sequence[str]) -> ET.Element:
    coverage = ET.Element("coverage")
    coverage.set("timestamp", timestamp_seconds(report))
    set_coverage_totals(coverage, report)

    sources_el = ET.SubElement(coverage, "sources")
    roots = list(source_roots) if source_roots else ["."]
    for root in roots:
        ET.SubElement(sources_el, "source").text = root

    packages_el = ET.SubElement(coverage, "packages")
    for kover_package in report.findall("package"):
        packages_el.append(convert_package(kover_package))

    return coverage


def load_kover_report(path: Path) -> ET.Element:
    if not path.is_file():
        raise FileNotFoundError(f"Kover report not found: {path}")
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid Kover XML in {path}: {exc}") from exc
    root = tree.getroot()
    if root.tag != "report":
        raise ValueError(
            f"Expected a Kover/JaCoCo <report> root in {path}, found <{root.tag}>"
        )
    return root


def write_cobertura(coverage: ET.Element, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(coverage)
    if hasattr(ET, "indent"):
        ET.indent(tree, space="  ")
    with path.open("wb") as handle:
        handle.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(handle, encoding="utf-8", xml_declaration=False)


def print_summary(coverage: ET.Element, output: Path) -> None:
    line_rate = float(coverage.get("line-rate", "0"))
    branch_rate = float(coverage.get("branch-rate", "0"))
    lines_covered = coverage.get("lines-covered", "0")
    lines_valid = coverage.get("lines-valid", "0")
    packages = coverage.find("packages")
    package_count = len(list(packages)) if packages is not None else 0
    print(
        f"Wrote Cobertura report to {output} "
        f"({package_count} packages, "
        f"line-rate={line_rate:.2%} [{lines_covered}/{lines_valid}], "
        f"branch-rate={branch_rate:.2%})"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input)
    output_path = Path(args.output)

    report = load_kover_report(input_path)
    coverage = convert_report(report, args.source_roots)
    write_cobertura(coverage, output_path)
    print_summary(coverage, output_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
