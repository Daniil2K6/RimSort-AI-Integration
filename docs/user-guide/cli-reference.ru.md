---
title: Справочник CLI
nav_order: 8
layout: default
parent: Руководство пользователя
permalink: user-guide/cli-reference
lang: ru
---

# CLI

{: .no_toc}

Командная строка для headless-режима: автоматизация, CI/CD, скрипты без GUI.

## Содержание

{: .no_toc .text-delta }

1. TOC
{:toc}

## Обзор

CLI RimSort для серверов и контейнеров:

- **Без display server** — Docker, CI/CD
- **Коды выхода** — для скриптов
- **Переменные окружения** — ключи без истории shell
- **Без Qt** — минимальные зависимости

Сейчас доступны: `build-db` — сборка метаданных Workshop, `mcp` — MCP-сервер для AI-агентов, `steam-login` — интерактивный вход в SteamCMD, `update-mods` — headless-обновление модов через SteamCMD.

## Запуск

Релиз:

```bash
./RimSort build-db --help
```

Windows:

```bash
RimSort.exe build-db --help
```

Из исходников:

```bash
python -m app build-db --help
uv run python -m app build-db --help
```

## Команды

### `build-db`

Сборка Steam Workshop DB через WebAPI: имена, URL, зависимости, опционально DLC. JSON совместим с RimSort и RimPy.

#### Требования

**Steam WebAPI Key**
{: .d-inline-block}
Обязательно
{: .label .label-red }

{: .important }
Ключ WebAPI — в [Сборщик БД](db-builder#как-получить-steam-webapi-ключ).

{: .warning}
Нужна RimWorld в Steam. Обычно $5+ на аккаунте для WebAPI и Steamworks (DLC).

Ключ — 32 символа.

#### Примеры

```bash
export RIMSORT_STEAM_API_KEY=your_32_character_key_here
RimSort build-db --output steamDB.json

RimSort build-db --output steamDB.json --no-dlc-data --quiet

RimSort build-db --output steamDB.json --update
```

#### Опции

| Опция | Тип | По умолчанию | Описание |
|--------|------|---------|-------------|
| `--api-key TEXT` | String | (см. ниже) | WebAPI ключ или `RIMSORT_STEAM_API_KEY` |
| `--output PATH` | Path | **обязательно** | Путь к JSON |
| `--dlc-data/--no-dlc-data` | Boolean | dlc-data | DLC через Steamworks (медленнее) |
| `--update/--overwrite` | Boolean | overwrite | Обновить или перезаписать |
| `--quiet` | Flag | false | Без прогресса в stdout |

#### Ключ API

1. `--api-key` — высший приоритет (история shell)
2. `RIMSORT_STEAM_API_KEY` — рекомендуется
3. `settings.json` из GUI

#### Коды выхода

- **0** — успех
- **1** — ошибка
- **2** — Ctrl+C

### `steam-login`

Интерактивный вход в SteamCMD под ваш Steam-аккаунт. Пароль и Steam Guard код вы вводите прямо в prompt SteamCMD — RimSort их не видит и не сохраняет.

{: .note }
Для модов Workshop RimWorld достаточно `anonymous`. Реальный аккаунт нужен только для приватных предметов, загрузки депо или если SteamCMD по другой причине отказывается от anonymous.

```bash
uv run python -m app steam-login my_account
```

| Опция | Тип | По умолчанию | Описание |
|--------|------|---------|-------------|
| `--no-install` | Flag | off | Не скачивать SteamCMD, а завершиться ошибкой, если его нет. |

Для последующих неинтерактивных запусков SteamCMD может снова спросить пароль: передайте его через переменную окружения `RIMSORT_STEAM_PASSWORD` (и `RIMSORT_STEAM_GUARD_CODE` для первого Steam Guard). Пароли никогда не пишутся в `settings.json` и в логи.

### `update-mods`

Проверка Steam WebAPI на устаревшие моды Workshop/SteamCMD и их обновление через SteamCMD — подходит для плановых автообновлений (cron/launchd) и серверов без GUI.

```bash
# Что будет обновлено (ничего не качает)
uv run python -m app update-mods --dry-run

# Обновить всё устаревшее (SteamCMD установится при необходимости)
uv run python -m app update-mods --install-steamcmd

# Под своим аккаунтом, явные цели
uv run python -m app update-mods --login my_account --pfid 1234567890
```

| Опция | Тип | По умолчанию | Описание |
|--------|------|---------|-------------|
| `--login TEXT` | String | `anonymous` | Аккаунт SteamCMD (сначала `steam-login`). |
| `--outdated-only` / `--all` | Flag | `--outdated-only` | Только устаревшие относительно Workshop / все цели. |
| `--pfid ID` | Multi | — | Явный publishedfileid (повторяется). |
| `--package-id ID` | Multi | — | Установленный packageId (повторяется). |
| `--sources TEXT` | String | `Steam CMD` | Фильтр проверки устаревания: `Steam CMD`, `Steam Workshop` или `all`. |
| `--install-steamcmd` | Flag | off | Скачать SteamCMD, если его нет. |
| `--validate` / `--no-validate` | Flag | настройка | Принудительный `validate` при загрузке. |
| `--dry-run` | Flag | off | Только разрешить цели, ничего не качать. |

Печатает JSON-отчёт и выходит с кодом != 0, если часть модов не обновилась. Пример ежедневного расписания:

```bash
0 6 * * * cd /path/to/RimSort && uv run python -m app update-mods >> /tmp/rimsort-update.log 2>&1
```

### Устранение проблем

##### **`Error: Steam API key is required`**

Укажите ключ: `--api-key`, `RIMSORT_STEAM_API_KEY` или в GUI.

##### **`Error: Invalid Steam WebAPI key! Key must be 32 characters`**

Проверьте длину на [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey).

##### **`Error: Cannot update non-existent database`**

Первый запуск — `--overwrite` (по умолчанию):

```bash
RimSort build-db --output steamDB.json --overwrite
RimSort build-db --output steamDB.json --update
```

##### **DLC не собирается**

Нужны Steam + RimWorld. Headless: `--no-dlc-data`.

```bash
RimSort build-db --output steamDB.json --no-dlc-data
```
