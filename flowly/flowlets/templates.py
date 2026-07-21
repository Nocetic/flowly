"""Ready-made flowlets — a one-tap start for the shapes people ask for most.

A template is a bundled definition plus the metadata a picker needs. Creating
from one just writes an ordinary flowlet the user owns: there is no lasting link
back, so the agent can reshape it afterwards like any other screen. That is the
whole point — a template is a starting position, not a managed thing.

Every user-facing string is authored in the three languages the apps ship and
resolved at BUILD time from the caller's own locale. A template should hand
someone a screen in their language, not one to translate.
"""

from __future__ import annotations

from typing import Callable, NamedTuple

LANGS = ("en", "tr", "es")
_FALLBACK = "en"

# (en, tr, es) → the one string for the language being built.
Say = Callable[[str, str, str], str]


def normalize_lang(lang: str | None) -> str:
    """A client's locale — ``"tr-TR"``, ``"TR"``, ``None`` — to one of LANGS."""
    code = (lang or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in LANGS else _FALLBACK


def _sayer(lang: str) -> Say:
    idx = LANGS.index(lang)

    def say(en: str, tr: str, es: str) -> str:
        return (en, tr, es)[idx]

    return say


# ── the templates ─────────────────────────────────────────────────────────────
# Each builder returns the BODY of a definition. `catalog`, `name`, `icon` and
# `accent` are filled from the registry entry, so the card a user picks and the
# flowlet they end up with can never disagree.

def _water(say: Say) -> dict:
    return {
        "state": {"goal_ml": {"type": "number", "default": 2000, "min": 250, "max": 6000}},
        "series": {"water": {"unit": "ml"}},
        "computed": {
            "today_ml": {"series": "water", "agg": "sum", "window": "today"},
            "remaining": {"expr": "max(0, goal_ml - today_ml)"},
            "status": {
                "cases": [{
                    "when": "today_ml >= goal_ml",
                    "text": say("Goal reached", "Hedefe ulaştın", "Objetivo alcanzado"),
                }],
                "else": say("{remaining} ml to go", "{remaining} ml kaldı",
                            "Faltan {remaining} ml"),
            },
        },
        "layout": [
            {"id": "bar", "type": "progress", "value": "today_ml", "max": "goal_ml",
             "label": "{today_ml} / {goal_ml} ml"},
            {"type": "text", "text": "{status}"},
            {"type": "row", "children": [
                {"id": "add250", "type": "button", "text": "+250 ml", "style": "primary",
                 "action": {"op": "log", "series": "water", "value": 250}},
                {"id": "add500", "type": "button", "text": "+500 ml",
                 "action": {"op": "log", "series": "water", "value": 500}},
            ]},
            {"id": "undoLast", "type": "button",
             "text": say("Undo last", "Sonuncuyu geri al", "Deshacer el último"),
             "action": {"op": "remove_last", "series": "water"}},
            {"id": "trend", "type": "chart", "kind": "bar",
             "data": {"series": "water", "agg": "sum", "bucket": "day", "window": "7d"}},
        ],
        "watches": [{
            "id": "evening_nudge", "trigger": "condition",
            "when": "today_ml < goal_ml", "after": "18:00", "cooldownMinutes": 120,
            "notify": {
                "title": say("Water", "Su", "Agua"),
                "body": say("{today_ml}/{goal_ml} ml today",
                            "Bugün {today_ml}/{goal_ml} ml",
                            "Hoy {today_ml}/{goal_ml} ml"),
            },
        }],
    }


def _habits(say: Say) -> dict:
    return {
        "state": {
            "first_done": {"type": "bool", "default": False},
            "second_done": {"type": "bool", "default": False},
        },
        "series": {"days": {}},
        "computed": {
            "both_done": {"expr": "first_done > 0 and second_done > 0"},
            "weekly_total": {"series": "days", "agg": "sum", "window": "7d"},
        },
        "layout": [
            {"id": "habits", "type": "checklist", "items": [
                {"key": "first_done",
                 "label": say("Move for 30 minutes", "30 dakika hareket et",
                              "Moverse 30 minutos")},
                {"key": "second_done",
                 "label": say("Read for 30 minutes", "30 dakika kitap oku",
                              "Leer 30 minutos")},
            ]},
            {"id": "completeDay", "type": "button", "style": "primary",
             "text": say("Complete the day", "Günü tamamla", "Completar el día"),
             "visibleWhen": "both_done > 0",
             "action": {"op": "batch", "once": "day", "ops": [
                 {"op": "log", "series": "days", "value": 1},
                 {"op": "reset", "key": "first_done"},
                 {"op": "reset", "key": "second_done"},
             ]}},
            {"type": "metric", "value": "weekly_total",
             "label": say("This week", "Bu hafta", "Esta semana")},
            {"id": "trend", "type": "chart", "kind": "bar",
             "data": {"series": "days", "agg": "sum", "bucket": "day", "window": "7d"}},
        ],
        "watches": [{
            "id": "evening_nudge", "trigger": "schedule", "at": "21:00",
            "notify": {
                "title": say("Habits", "Alışkanlıklar", "Hábitos"),
                "body": say("How did today go?", "Bugün nasıl geçti?",
                            "¿Cómo fue el día?"),
            },
        }],
    }


def _expenses(say: Say) -> dict:
    categories = [
        say("Food", "Yemek", "Comida"),
        say("Transport", "Ulaşım", "Transporte"),
        say("Bills", "Faturalar", "Facturas"),
        say("Other", "Diğer", "Otros"),
    ]
    return {
        "state": {
            "expenses": {"type": "list", "max": 200, "item": {
                "title": "string", "amount": "number", "category": "string",
                "date": "date", "receipt": "image",
            }},
        },
        "computed": {
            "month_total": {"list": "expenses", "agg": "sum", "field": "amount",
                            "where": "days_since(date) < 30"},
        },
        "layout": [
            {"id": "spend", "type": "tracker_card", "list": "expenses", "field": "amount",
             "title": say("This month", "Bu ay", "Este mes"),
             "window": "30d", "chart": "bar"},
            {"id": "receiptShot", "type": "photo",
             "label": say("Add from a receipt", "Fişten ekle", "Añadir desde un recibo"),
             "action": {"op": "vision", "into": "expenses", "prompt": say(
                 "Read this receipt. Return the merchant as the title, the total as "
                 "the amount, a sensible category, and the date on the receipt.",
                 "Bu fişi oku. Başlık olarak satıcıyı, tutar olarak toplamı, uygun bir "
                 "kategoriyi ve fişteki tarihi döndür.",
                 "Lee este recibo. Devuelve el comercio como título, el total como "
                 "importe, una categoría adecuada y la fecha del recibo.",
             )}},
            {"id": "addExpense", "type": "form", "into": "expenses",
             "title": say("Add expense", "Harcama ekle", "Añadir gasto"),
             "fields": [
                 {"field": "title", "label": say("What", "Ne", "Qué")},
                 {"field": "amount", "label": say("Amount", "Tutar", "Importe")},
                 {"field": "category", "options": categories},
                 {"field": "date", "default": "today"},
             ],
             "submit": {"label": say("Add", "Ekle", "Añadir")}},
            {"type": "repeater", "source": "expenses",
             "empty": say("No expenses yet", "Henüz harcama yok", "Sin gastos aún"),
             "item": {"type": "list_row", "thumb": "$.receipt", "title": "$.title",
                      "subtitle": "$.category", "badge": "$.date",
                      "value": "{$.amount}"}},
        ],
    }


def _tasks(say: Say) -> dict:
    return {
        "state": {
            "tasks": {"type": "list", "max": 200,
                      "item": {"title": "string", "done": "bool"}},
        },
        "computed": {
            "open_count": {"list": "tasks", "agg": "count", "where": "done == 0"},
        },
        "layout": [
            {"id": "addTask", "type": "form", "into": "tasks",
             "fields": [{"field": "title", "label": say("Task", "Görev", "Tarea")}],
             "submit": {"label": say("Add", "Ekle", "Añadir")}},
            {"type": "metric", "value": "open_count",
             "label": say("Still open", "Kalan", "Pendientes")},
            {"type": "repeater", "source": "tasks",
             "empty": say("Nothing here yet", "Burada henüz bir şey yok",
                          "Nada por aquí todavía"),
             "item": {"type": "row", "children": [
                 {"id": "toggleDone", "type": "toggle", "value": "$.done",
                  "action": {"op": "item_toggle", "key": "tasks", "field": "done"}},
                 {"type": "list_row", "title": "$.title"},
             ]}},
        ],
    }


class Template(NamedTuple):
    id: str
    icon: str
    accent: str
    title: tuple[str, str, str]
    description: tuple[str, str, str]
    build: Callable[[Say], dict]


TEMPLATES: tuple[Template, ...] = (
    Template(
        id="water", icon="droplet", accent="#00A6C8",
        title=("Water", "Su Takibi", "Agua"),
        description=(
            "A daily goal, one tap per glass, and a week at a glance.",
            "Günlük hedef, her bardak için tek dokunuş ve bir haftalık görünüm.",
            "Un objetivo diario, un toque por vaso y la semana de un vistazo.",
        ),
        build=_water,
    ),
    Template(
        id="habits", icon="check", accent="#7C6FF0",
        title=("Habits", "Alışkanlıklar", "Hábitos"),
        description=(
            "Two habits to tick off, an evening nudge, and a weekly streak.",
            "İşaretlenecek iki alışkanlık, akşam hatırlatması ve haftalık seri.",
            "Dos hábitos que marcar, un aviso por la noche y la racha semanal.",
        ),
        build=_habits,
    ),
    Template(
        id="expenses", icon="receipt", accent="#E2703A",
        title=("Expenses", "Harcamalar", "Gastos"),
        description=(
            "Log spending by hand or straight from a receipt photo.",
            "Harcamayı elle ya da doğrudan fiş fotoğrafından kaydet.",
            "Registra gastos a mano o directamente desde una foto del recibo.",
        ),
        build=_expenses,
    ),
    Template(
        id="tasks", icon="list", accent="#2E9E6B",
        title=("Tasks", "Görevler", "Tareas"),
        description=(
            "A running list you add to, tick off, and swipe away.",
            "Eklediğin, işaretlediğin ve kaydırıp sildiğin bir liste.",
            "Una lista a la que añades, marcas y deslizas para borrar.",
        ),
        build=_tasks,
    ),
)

_BY_ID = {t.id: t for t in TEMPLATES}


def list_templates(lang: str | None = None) -> list[dict]:
    """The picker's cards — metadata only, no definitions (a picker never needs
    to know what a flowlet is made of)."""
    i = LANGS.index(normalize_lang(lang))
    return [
        {"id": t.id, "title": t.title[i], "description": t.description[i],
         "icon": t.icon, "accent": t.accent}
        for t in TEMPLATES
    ]


def build_template(template_id: str, lang: str | None = None) -> dict:
    """One template as a complete, ready-to-validate definition. Raises KeyError
    for an unknown id."""
    tpl = _BY_ID.get(str(template_id or ""))
    if tpl is None:
        raise KeyError(template_id)
    code = normalize_lang(lang)
    defn = dict(tpl.build(_sayer(code)))
    defn["catalog"] = 3
    defn["name"] = tpl.title[LANGS.index(code)]
    defn["icon"] = tpl.icon
    defn["accent"] = tpl.accent
    return defn
