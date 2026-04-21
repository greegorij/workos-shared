# Contributing

Wytyczne dla pracy z tym repozytorium — polityka zależności, wersjonowanie, testy i workflow.

## Polityka zerowych zależności zewnętrznych

`workos-shared` używa wyłącznie biblioteki standardowej Pythona (stdlib). Żaden moduł nie może importować paczek z PyPI.

**Cel:** konsumenci (`jarvis-infra`, `fireflies-agent` itp.) mogą zainstalować `workos-shared` bez ryzyka konfliktów wersji lub dodatkowych zależności.

Dozwolone importy:

```python
import urllib.request   # zamiast requests / httpx
import sqlite3          # zamiast sqlalchemy / databases
import json, pathlib, logging, threading, time  # stdlib ok
```

Zabronione:

```python
import requests         # nie
import httpx            # nie
import sqlalchemy       # nie
import pydantic         # nie
```

Jeśli potrzebujesz zewnętrznej biblioteki — zaimplementuj lekki wrapper stdlib lub umieść logikę w repo konsumenta (np. `jarvis-infra`).

## Wersjonowanie SemVer

Projekt stosuje [Semantic Versioning](https://semver.org/):

| Zmiana | Bump wersji | Przykład |
|---|---|---|
| Niekompatybilna zmiana API (breaking change) | MAJOR | `0.2.0 → 1.0.0` |
| Nowy moduł lub nowa funkcja (backwards-compatible) | MINOR | `0.2.0 → 0.3.0` |
| Bugfix, poprawa wewnętrzna | PATCH | `0.2.0 → 0.2.1` |

### Checklist bumpu wersji

Przy każdej zmianie wersji zaktualizuj **oba** pliki synchronicznie:

```toml
# pyproject.toml
[project]
version = "X.Y.Z"
```

```python
# workos_shared/__init__.py
__version__ = "X.Y.Z"
```

Weryfikacja przed commitem:

```bash
python -c "import workos_shared; print(workos_shared.__version__)"
```

Numer wersji w obu plikach musi być identyczny.

## Wymagania dotyczące testów

Każdy moduł musi mieć:

- Minimum **10 testów** w `tests/test_<module>.py`
- Pokrycie **80%+** funkcjonalnej powierzchni modułu (ścieżki sukcesu, error handling, edge cases)

Struktura testów:

```
tests/
  test_logger.py       # workos_shared.logger
  test_openrouter.py   # workos_shared.openrouter
  test_<new_module>.py # każdy nowy moduł
```

Uruchomienie testów:

```bash
# Z coverage
pytest --cov=workos_shared --cov-report=term-missing tests/

# Szybki run
pytest tests/ -q
```

Wymagania dotyczące coverage egzekwowane ręcznie — sprawdź raport przed otwarciem PR.

## Workflow z pull requestami

```bash
# 1. Aktualizacja main + nowy branch
git checkout main && git pull origin main
git checkout -b feat/<moduł>-<krótki-opis>

# 2. Implementacja + testy
pytest tests/ -v

# 3. Bump wersji (patrz wyżej) + weryfikacja
python -c "import workos_shared; print(workos_shared.__version__)"

# 4. Push i PR
git push -u origin feat/<moduł>-<krótki-opis>
gh pr create --base main
```

Merge: wyłącznie **squash merge**.

Konwencja commitów — Conventional Commits:

```
feat(<module>): add <new_module> stdlib implementation
fix(logger): handle missing WEBHOOK_URL gracefully
docs(openrouter): update quickstart example in MIGRATION.md
chore: bump version to 0.3.0
```

## Powiązane

- [README.md](README.md) — przegląd modułów, design rules, quick start
- [docs/MIGRATION.md](docs/MIGRATION.md) — runbook adopcji w serwisach konsumenckich
