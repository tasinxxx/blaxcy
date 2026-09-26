"""Packaging integrity (specification section 87: installability).

The distribution has to be installable and honest about itself: every package it
declares must exist, every module it exposes must import, its chosen entry point
must resolve to a callable, and its declared dependency set must match what the
code needs. These checks are the cheap, deterministic half of packaging
validation; the build/install/uninstall round trip is covered by the CI
``package`` job (and was verified by hand: wheel build, clean-venv install,
imports, ``blaxcy --help``, clean uninstall).

Two rules from the specification are pinned here because they are easy to break
silently:

* versions are lower-bounded, never pinned (arbitrary pins are forbidden); and
* the default developer set must not require a plugin the suite does not use --
  the section 71 GUI tests build their own offscreen ``QApplication`` and never
  request the ``qtbot`` fixture, so requiring ``pytest-qt`` only added a way for
  a bare ``pytest`` to abort on a host whose Qt binding ships no ``QtTest``.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import tomlkit

_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _ROOT / "pyproject.toml"


def _config() -> object:
    """The parsed ``pyproject.toml``."""
    return tomlkit.parse(_PYPROJECT.read_text(encoding="utf-8"))


def _dotted_to_path(dotted: str) -> Path:
    return _ROOT.joinpath(*dotted.split("."))


def test_declared_packages_all_exist_with_an_init() -> None:
    """Every package the wheel ships exists on disk with an ``__init__``."""
    config = _config()
    packages = config["tool"]["setuptools"]["packages"]  # type: ignore[index]
    for package in packages:
        assert _dotted_to_path(str(package)).joinpath("__init__.py").is_file(), (
            f"declared package {package!r} does not exist"
        )


def test_py_modules_exist() -> None:
    """Every ``py-modules`` entry resolves to a real module file."""
    config = _config()
    modules = config["tool"]["setuptools"]["py-modules"]  # type: ignore[index]
    for module in modules:
        assert _ROOT.joinpath(f"{module}.py").is_file(), f"missing py-module {module!r}"


def test_every_importable_top_level_package_is_declared() -> None:
    """A package importable from the source tree must be declared, so the wheel has it."""
    config = _config()
    declared = {str(p) for p in config["tool"]["setuptools"]["packages"]}  # type: ignore[index]
    for child in _ROOT.iterdir():
        if not child.is_dir() or not child.joinpath("__init__.py").is_file():
            continue
        name = child.name
        if name == "tests":
            # The test package is deliberately not shipped.
            continue
        assert name in declared, f"package {name!r} is importable but not declared in [tool.setuptools]"


def test_the_console_script_resolves_to_a_callable() -> None:
    """``blaxcy = main:main`` must import and name a callable, or the CLI is dead on arrival."""
    config = _config()
    scripts = config["project"]["scripts"]  # type: ignore[index]
    for target in scripts.values():
        module_name, _, attribute = str(target).partition(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute)), f"{target!r} is not a callable"


def test_runtime_dependencies_are_lower_bounded_not_pinned() -> None:
    """Section 4: versions are lower-bounded, not pinned arbitrarily."""
    config = _config()
    for dependency in config["project"]["dependencies"]:  # type: ignore[index]
        text = str(dependency)
        assert "==" not in text, f"runtime dependency {text!r} pins a version"


def test_pytest_qt_is_optional_not_a_default_dev_dependency() -> None:
    """The suite does not use ``qtbot``; requiring the plugin only risks aborting collection."""
    config = _config()
    extras = config["project"]["optional-dependencies"]  # type: ignore[index]
    dev = [str(item) for item in extras["dev"]]
    assert "pytest-qt" not in dev, "pytest-qt must not be in the default dev set"
    assert "pytest-qt" in [str(item) for item in extras["gui-test"]], (
        "pytest-qt should remain available as the opt-in gui-test extra"
    )


def test_no_test_requests_the_qtbot_fixture() -> None:
    """Removing ``pytest-qt`` from the defaults is only safe while nothing uses it."""
    this_file = Path(__file__).resolve()
    for path in (_ROOT / "tests").rglob("*.py"):
        if path.resolve() == this_file:
            continue  # this test names the fixture to assert nobody else does
        text = path.read_text(encoding="utf-8")
        assert "qtbot" not in text, f"{path} requests the qtbot fixture; keep pytest-qt installed"
