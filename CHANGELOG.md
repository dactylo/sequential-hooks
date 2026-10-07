# Changelog

## 0.1.1 (2026-10-06)

### Changed

- `doctor` shows executable and configuration paths with forward slashes, including on Windows.

### Fixed

- `--help` and `doctor --help` now describe every option.
- Type checkers accept any collection of strings for `Composer.bounded`, so adapter code that
  passes a tuple or set type-checks.

### Security

- On Windows, `doctor` no longer looks up executables given as NT object-manager paths (`\??\...`),
  which could contact a network share named by the inspected configuration.

## 0.1.0 (2026-09-29)

Initial release.
