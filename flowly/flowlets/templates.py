"""Ready-made flowlets — a one-tap start for the shapes people ask for most.

A template is a bundled definition plus the metadata a picker needs. Creating
from one just writes an ordinary flowlet the user owns: there is no lasting link
back, so the agent can reshape it afterwards like any other screen. That is the
whole point — a template is a starting position, not a managed thing.

Each one is built to look like a screen someone designed rather than a demo: a
headline the eye lands on first, the controls that act on it, then the history
underneath. They lean on the catalog's own display components (ring, stat,
callout, heatmap) so a new user sees what a flowlet can actually be.

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
            "week_avg": {"series": "water", "agg": "avg", "window": "7d"},
            "glasses": {"expr": "round(today_ml / 250)"},
            "status": {
                "cases": [
                    {"when": "today_ml >= goal_ml",
                     "text": say("Goal reached — nice one.", "Hedefe ulaştın — helal.",
                                 "Objetivo alcanzado, bien hecho.")},
                    {"when": "today_ml == 0",
                     "text": say("Nothing logged yet today.", "Bugün henüz kayıt yok.",
                                 "Aún no has registrado nada hoy.")},
                ],
                "else": say("{remaining} ml to go.", "{remaining} ml kaldı.",
                            "Faltan {remaining} ml."),
            },
        },
        "layout": [
            {"type": "card", "children": [
                {"type": "header", "text": say("Today", "Bugün", "Hoy")},
                {"id": "todayRing", "type": "ring", "value": "today_ml", "max": "goal_ml",
                 "label": "{today_ml} ml"},
                {"type": "row", "children": [
                    {"type": "stat", "value": "glasses",
                     "label": say("Glasses", "Bardak", "Vasos")},
                    {"type": "stat", "value": "remaining",
                     "label": say("Left (ml)", "Kalan (ml)", "Restante (ml)")},
                ]},
                {"type": "text", "text": "{status}"},
            ]},
            {"type": "row", "children": [
                {"id": "add250", "type": "button", "text": "+250 ml", "style": "primary",
                 "action": {"op": "log", "series": "water", "value": 250}},
                {"id": "add500", "type": "button", "text": "+500 ml",
                 "action": {"op": "log", "series": "water", "value": 500}},
            ]},
            {"id": "undoLast", "type": "button",
             "text": say("Undo last", "Sonuncuyu geri al", "Deshacer el último"),
             "action": {"op": "remove_last", "series": "water"}},
            {"id": "goalMet", "type": "callout", "visibleWhen": "today_ml >= goal_ml",
             "text": say("You hit today's goal. Everything past this is a bonus.",
                         "Bugünkü hedefi tutturdun. Bundan sonrası bonus.",
                         "Has alcanzado el objetivo de hoy. Lo demás es extra.")},
            {"type": "divider"},
            {"type": "header", "text": say("This week", "Bu hafta", "Esta semana")},
            {"id": "trend", "type": "chart", "kind": "bar",
             "data": {"series": "water", "agg": "sum", "bucket": "day", "window": "7d"}},
            {"type": "stat", "value": "week_avg",
             "label": say("Daily average (ml)", "Günlük ortalama (ml)",
                          "Promedio diario (ml)")},
            {"type": "divider"},
            {"id": "goalInput", "type": "number_input", "value": "goal_ml", "step": 250,
             "label": say("Daily goal (ml)", "Günlük hedef (ml)", "Objetivo diario (ml)"),
             "action": {"op": "set", "key": "goal_ml"}},
        ],
        "watches": [
            {"id": "evening_nudge", "trigger": "condition",
             "when": "today_ml < goal_ml", "after": "18:00", "cooldownMinutes": 120,
             "notify": {
                 "title": say("Water", "Su", "Agua"),
                 "body": say("{today_ml}/{goal_ml} ml today",
                             "Bugün {today_ml}/{goal_ml} ml",
                             "Hoy {today_ml}/{goal_ml} ml"),
             }},
            {"id": "goal_hit", "trigger": "goal", "when": "today_ml >= goal_ml",
             "notify": {
                 "title": say("Water", "Su", "Agua"),
                 "body": say("Daily goal reached.", "Günlük hedef tamam.",
                             "Objetivo diario alcanzado."),
             }},
        ],
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
            "monthly_total": {"series": "days", "agg": "sum", "window": "30d"},
            "status": {
                "cases": [
                    {"when": "both_done > 0",
                     "text": say("Both done — close the day.", "İkisi de tamam — günü kapat.",
                                 "Ambos hechos: cierra el día.")},
                    {"when": "first_done > 0 or second_done > 0",
                     "text": say("One to go.", "Bir tane kaldı.", "Falta uno.")},
                ],
                "else": say("Nothing ticked yet.", "Henüz işaretlenmedi.",
                            "Nada marcado aún."),
            },
        },
        "layout": [
            {"type": "card", "children": [
                {"type": "header", "text": say("Today", "Bugün", "Hoy")},
                {"id": "habits", "type": "checklist", "items": [
                    {"key": "first_done",
                     "label": say("Move for 30 minutes", "30 dakika hareket et",
                                  "Moverse 30 minutos")},
                    {"key": "second_done",
                     "label": say("Read for 30 minutes", "30 dakika kitap oku",
                                  "Leer 30 minutos")},
                ]},
                {"type": "text", "text": "{status}"},
                {"id": "completeDay", "type": "button", "style": "primary",
                 "text": say("Complete the day", "Günü tamamla", "Completar el día"),
                 "visibleWhen": "both_done > 0",
                 "action": {"op": "batch", "once": "day", "ops": [
                     {"op": "log", "series": "days", "value": 1},
                     {"op": "reset", "key": "first_done"},
                     {"op": "reset", "key": "second_done"},
                 ]}},
            ]},
            {"type": "row", "children": [
                {"id": "weekRing", "type": "ring", "value": "weekly_total", "max": 7,
                 "label": "{weekly_total}/7"},
                {"type": "stat", "value": "monthly_total",
                 "label": say("Days this month", "Bu ay gün", "Días este mes")},
            ]},
            {"type": "divider"},
            {"type": "header", "text": say("History", "Geçmiş", "Historial")},
            {"id": "grid", "type": "heatmap",
             "data": {"series": "days", "agg": "sum", "bucket": "day", "window": "90d"}},
            {"id": "trend", "type": "chart", "kind": "bar",
             "data": {"series": "days", "agg": "sum", "bucket": "week", "window": "90d"}},
        ],
        "watches": [
            {"id": "evening_nudge", "trigger": "schedule", "at": "21:00",
             "notify": {
                 "title": say("Habits", "Alışkanlıklar", "Hábitos"),
                 "body": say("How did today go?", "Bugün nasıl geçti?",
                             "¿Cómo fue el día?"),
             }},
            {"id": "gone_stale", "trigger": "stale", "series": "days", "idleMinutes": 2880,
             "notify": {
                 "title": say("Habits", "Alışkanlıklar", "Hábitos"),
                 "body": say("Two days without a check-in.",
                             "İki gündür kayıt yok.",
                             "Dos días sin registro."),
             }},
        ],
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
            "monthly_budget": {"type": "number", "default": 15000, "min": 0, "max": 1000000},
            "expenses": {"type": "list", "max": 200, "item": {
                "title": "string", "amount": "number", "category": "string",
                "date": "date", "receipt": "image",
            }},
        },
        "computed": {
            "month_total": {"list": "expenses", "agg": "sum", "field": "amount",
                            "where": "days_since(date) < 30"},
            "month_count": {"list": "expenses", "agg": "count",
                            "where": "days_since(date) < 30"},
            "biggest": {"list": "expenses", "agg": "max", "field": "amount",
                        "where": "days_since(date) < 30"},
            "remaining": {"expr": "max(0, monthly_budget - month_total)"},
            "over_by": {"expr": "max(0, month_total - monthly_budget)"},
            "status": {
                "cases": [
                    {"when": "month_total > monthly_budget",
                     "text": say("Over budget by {over_by}.", "Bütçeyi {over_by} aştın.",
                                 "Te has pasado por {over_by}.")},
                    {"when": "month_total == 0",
                     "text": say("Nothing logged this month.", "Bu ay kayıt yok.",
                                 "Nada registrado este mes.")},
                ],
                "else": say("{remaining} left this month.", "Bu ay {remaining} kaldı.",
                            "Quedan {remaining} este mes."),
            },
        },
        "layout": [
            {"id": "spend", "type": "tracker_card", "list": "expenses", "field": "amount",
             "title": say("This month", "Bu ay", "Este mes"),
             "window": "30d", "chart": "bar"},
            {"type": "row", "children": [
                {"type": "stat", "value": "remaining",
                 "label": say("Left", "Kalan", "Restante")},
                {"type": "stat", "value": "month_count",
                 "label": say("Entries", "Kayıt", "Registros")},
                {"type": "stat", "value": "biggest",
                 "label": say("Biggest", "En büyük", "Mayor")},
            ]},
            {"type": "text", "text": "{status}"},
            {"id": "overBudget", "type": "callout", "visibleWhen": "month_total > monthly_budget",
             "text": say("You're past the budget for this month.",
                         "Bu ayın bütçesini aştın.",
                         "Has superado el presupuesto de este mes.")},
            {"type": "divider"},
            {"type": "header", "text": say("By category", "Kategoriye göre", "Por categoría")},
            {"id": "byCategory", "type": "chart", "kind": "donut",
             "data": {"list": "expenses", "agg": "sum", "field": "amount",
                      "groupBy": "category", "window": "30d"}},
            {"type": "divider"},
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
            {"type": "header", "text": say("Recent", "Son harcamalar", "Recientes")},
            {"type": "repeater", "source": "expenses", "sortBy": {"field": "date", "dir": "desc"},
             "empty": say("No expenses yet", "Henüz harcama yok", "Sin gastos aún"),
             "item": {"type": "list_row", "thumb": "$.receipt", "title": "$.title",
                      "subtitle": "$.category", "badge": "$.date",
                      "value": "{$.amount}"}},
            {"type": "divider"},
            {"id": "budgetInput", "type": "number_input", "value": "monthly_budget", "step": 500,
             "label": say("Monthly budget", "Aylık bütçe", "Presupuesto mensual"),
             "action": {"op": "set", "key": "monthly_budget"}},
        ],
    }


def _tasks(say: Say) -> dict:
    return {
        "state": {
            "tasks": {"type": "list", "max": 200,
                      "item": {"title": "string", "done": "bool", "due": "date"}},
        },
        "computed": {
            "open_count": {"list": "tasks", "agg": "count", "where": "done == 0"},
            "done_count": {"list": "tasks", "agg": "count", "where": "done > 0"},
            "total": {"list": "tasks", "agg": "count"},
            "status": {
                "cases": [
                    {"when": "total == 0",
                     "text": say("Add the first one.", "İlkini ekle.", "Añade la primera.")},
                    {"when": "open_count == 0",
                     "text": say("All clear.", "Hepsi bitti.", "Todo listo.")},
                ],
                "else": say("{open_count} left.", "{open_count} tane kaldı.",
                            "Quedan {open_count}."),
            },
        },
        "layout": [
            {"type": "card", "children": [
                {"type": "row", "children": [
                    {"type": "stat", "value": "open_count",
                     "label": say("Open", "Açık", "Pendientes")},
                    {"type": "stat", "value": "done_count",
                     "label": say("Done", "Biten", "Hechas")},
                ]},
                {"id": "doneBar", "type": "progress", "value": "done_count", "max": "total",
                 "label": "{done_count}/{total}"},
                {"type": "text", "text": "{status}"},
            ]},
            {"id": "allDone", "type": "callout",
             "visibleWhen": "total > 0 and open_count == 0",
             "text": say("Nothing left on the list.", "Listede bir şey kalmadı.",
                         "No queda nada en la lista.")},
            {"id": "addTask", "type": "form", "into": "tasks",
             "fields": [
                 {"field": "title", "label": say("Task", "Görev", "Tarea")},
                 {"field": "due", "label": say("Due", "Bitiş", "Vence")},
             ],
             "submit": {"label": say("Add", "Ekle", "Añadir")}},
            {"type": "divider"},
            {"type": "header", "text": say("To do", "Yapılacaklar", "Por hacer")},
            {"type": "repeater", "source": "tasks", "where": "done == 0",
             "empty": say("Nothing here yet", "Burada henüz bir şey yok",
                          "Nada por aquí todavía"),
             "item": {"type": "row", "children": [
                 {"id": "toggleOpen", "type": "toggle", "value": "$.done",
                  "action": {"op": "item_toggle", "key": "tasks", "field": "done"}},
                 {"type": "list_row", "title": "$.title", "badge": "$.due"},
             ]}},
            {"type": "header", "text": say("Done", "Bitenler", "Hechas")},
            {"type": "repeater", "source": "tasks", "where": "done > 0",
             "empty": say("Nothing finished yet", "Henüz biten yok",
                          "Nada terminado aún"),
             "item": {"type": "row", "children": [
                 {"id": "toggleDone", "type": "toggle", "value": "$.done",
                  "action": {"op": "item_toggle", "key": "tasks", "field": "done"}},
                 {"type": "list_row", "title": "$.title"},
             ]}},
        ],
    }


def _sleep(say: Say) -> dict:
    return {
        "state": {"goal_hours": {"type": "number", "default": 8, "min": 4, "max": 12}},
        "series": {"sleep": {"unit": "h"}},
        "computed": {
            "last_night": {"series": "sleep", "agg": "sum", "window": "today"},
            "week_avg": {"series": "sleep", "agg": "avg", "window": "7d"},
            "best": {"series": "sleep", "agg": "max", "window": "30d"},
            "debt": {"expr": "max(0, goal_hours - last_night)"},
            "status": {
                "cases": [
                    {"when": "last_night == 0",
                     "text": say("No sleep logged for today yet.",
                                 "Bugün için henüz uyku kaydı yok.",
                                 "Aún no has registrado el sueño de hoy.")},
                    {"when": "last_night >= goal_hours",
                     "text": say("A full night. Good.", "Tam bir gece. Güzel.",
                                 "Una noche completa. Bien.")},
                ],
                "else": say("{debt}h short of your goal.", "Hedefin {debt} saat altında.",
                            "Te faltan {debt}h para tu objetivo."),
            },
        },
        "layout": [
            {"type": "card", "children": [
                {"type": "header", "text": say("Last night", "Dün gece", "Anoche")},
                {"id": "nightRing", "type": "ring", "value": "last_night", "max": "goal_hours",
                 "label": "{last_night}h"},
                {"type": "row", "children": [
                    {"type": "stat", "value": "week_avg",
                     "label": say("7-day average", "7 gün ortalama", "Media de 7 días")},
                    {"type": "stat", "value": "best",
                     "label": say("Best (30d)", "En iyi (30g)", "Mejor (30d)")},
                ]},
                {"type": "text", "text": "{status}"},
            ]},
            {"id": "logHours", "type": "number_input", "step": 0.5,
             "label": say("Hours slept", "Uyunan saat", "Horas dormidas"),
             "action": {"op": "log", "series": "sleep"}},
            {"type": "row", "children": [
                {"id": "quick7", "type": "button", "text": "7h",
                 "action": {"op": "log", "series": "sleep", "value": 7}},
                {"id": "quick8", "type": "button", "text": "8h", "style": "primary",
                 "action": {"op": "log", "series": "sleep", "value": 8}},
            ]},
            {"id": "undoLast", "type": "button",
             "text": say("Undo last", "Sonuncuyu geri al", "Deshacer el último"),
             "action": {"op": "remove_last", "series": "sleep"}},
            {"id": "shortNight", "type": "callout", "visibleWhen": "last_night > 0 and debt > 1",
             "text": say("A short night — go easy on yourself today.",
                         "Kısa bir gece — bugün kendini fazla zorlama.",
                         "Una noche corta: tómatelo con calma hoy.")},
            {"type": "divider"},
            {"type": "header", "text": say("Last two weeks", "Son iki hafta",
                                           "Últimas dos semanas")},
            {"id": "trend", "type": "chart", "kind": "line",
             "data": {"series": "sleep", "agg": "sum", "bucket": "day", "window": "30d"}},
            {"type": "divider"},
            {"id": "goalInput", "type": "number_input", "value": "goal_hours", "step": 0.5,
             "label": say("Target hours", "Hedef saat", "Horas objetivo"),
             "action": {"op": "set", "key": "goal_hours"}},
        ],
        "watches": [{
            "id": "bedtime", "trigger": "schedule", "at": "23:00",
            "notify": {
                "title": say("Sleep", "Uyku", "Sueño"),
                "body": say("Winding down? Your target is {goal_hours}h.",
                            "Yatma vakti mi? Hedefin {goal_hours} saat.",
                            "¿Hora de dormir? Tu objetivo es {goal_hours}h."),
            },
        }],
    }


def _mood(say: Say) -> dict:
    return {
        "state": {
            "notes": {"type": "list", "max": 200,
                      "item": {"note": "string", "date": "date"}},
        },
        "series": {"mood": {}},
        "computed": {
            "today_mood": {"series": "mood", "agg": "max", "window": "today"},
            "week_avg": {"series": "mood", "agg": "avg", "window": "7d"},
            "month_avg": {"series": "mood", "agg": "avg", "window": "30d"},
            "checkins": {"series": "mood", "agg": "count", "window": "30d"},
            "status": {
                "cases": [
                    {"when": "today_mood == 0",
                     "text": say("How's today going?", "Bugün nasıl gidiyor?",
                                 "¿Cómo va hoy?")},
                    {"when": "today_mood >= 4",
                     "text": say("A good one. Worth a note.", "İyi bir gün. Not düşmeye değer.",
                                 "Un buen día. Merece una nota.")},
                    {"when": "today_mood <= 2",
                     "text": say("A heavy one. Be kind to yourself.",
                                 "Ağır bir gün. Kendine iyi davran.",
                                 "Un día duro. Sé amable contigo.")},
                ],
                "else": say("Logged for today.", "Bugün için kaydedildi.",
                            "Registrado para hoy."),
            },
        },
        "layout": [
            {"type": "card", "children": [
                {"type": "header", "text": say("How are you today?", "Bugün nasılsın?",
                                               "¿Cómo estás hoy?")},
                {"id": "todayMood", "type": "rating", "value": "today_mood", "max": 5,
                 "action": {"op": "log", "series": "mood"}},
                {"type": "text", "text": "{status}"},
            ]},
            {"type": "row", "children": [
                {"type": "stat", "value": "week_avg",
                 "label": say("7-day average", "7 gün ortalama", "Media de 7 días")},
                {"type": "stat", "value": "month_avg",
                 "label": say("30-day average", "30 gün ortalama", "Media de 30 días")},
                {"type": "stat", "value": "checkins",
                 "label": say("Check-ins", "Kayıt", "Registros")},
            ]},
            {"type": "divider"},
            {"type": "header", "text": say("Last 90 days", "Son 90 gün", "Últimos 90 días")},
            {"id": "grid", "type": "heatmap",
             "data": {"series": "mood", "agg": "avg", "bucket": "day", "window": "90d"}},
            {"id": "trend", "type": "chart", "kind": "area",
             "data": {"series": "mood", "agg": "avg", "bucket": "day", "window": "30d"}},
            {"type": "divider"},
            {"id": "addNote", "type": "form", "into": "notes",
             "title": say("Add a note", "Not ekle", "Añadir una nota"),
             "fields": [
                 {"field": "note", "label": say("What happened", "Ne oldu", "Qué pasó")},
                 {"field": "date", "default": "today"},
             ],
             "submit": {"label": say("Save", "Kaydet", "Guardar")}},
            {"type": "repeater", "source": "notes", "sortBy": {"field": "date", "dir": "desc"},
             "empty": say("No notes yet", "Henüz not yok", "Sin notas aún"),
             "item": {"type": "list_row", "title": "$.note", "badge": "$.date"}},
        ],
        "watches": [{
            "id": "evening_checkin", "trigger": "schedule", "at": "20:00",
            "notify": {
                "title": say("Mood", "Ruh hâli", "Ánimo"),
                "body": say("A quick check-in for today?",
                            "Bugün için kısa bir kayıt?",
                            "¿Un registro rápido de hoy?"),
            },
        }],
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
        id="habits", icon="flame", accent="#7C6FF0",
        title=("Habits", "Alışkanlıklar", "Hábitos"),
        description=(
            "Two habits to tick off, an evening nudge, and a streak you can see.",
            "İşaretlenecek iki alışkanlık, akşam hatırlatması ve görünen bir seri.",
            "Dos hábitos que marcar, un aviso por la noche y una racha visible.",
        ),
        build=_habits,
    ),
    Template(
        id="expenses", icon="wallet", accent="#E2703A",
        title=("Expenses", "Harcamalar", "Gastos"),
        description=(
            "A budget, a category breakdown, and entry straight from a receipt photo.",
            "Bütçe, kategori dağılımı ve doğrudan fiş fotoğrafından kayıt.",
            "Un presupuesto, un desglose por categoría y registro desde una foto.",
        ),
        build=_expenses,
    ),
    Template(
        id="tasks", icon="check", accent="#2E9E6B",
        title=("Tasks", "Görevler", "Tareas"),
        description=(
            "A running list you add to, tick off, and swipe away.",
            "Eklediğin, işaretlediğin ve kaydırıp sildiğin bir liste.",
            "Una lista a la que añades, marcas y deslizas para borrar.",
        ),
        build=_tasks,
    ),
    Template(
        id="sleep", icon="moon", accent="#5A6BD8",
        title=("Sleep", "Uyku", "Sueño"),
        description=(
            "Log the night, watch the two-week trend, get a bedtime nudge.",
            "Geceyi kaydet, iki haftalık eğilimi gör, yatma vakti hatırlatması al.",
            "Registra la noche, mira la tendencia y recibe un aviso para dormir.",
        ),
        build=_sleep,
    ),
    Template(
        id="mood", icon="heart", accent="#D9557F",
        title=("Mood", "Ruh Hâli", "Ánimo"),
        description=(
            "A one-tap daily check-in, 90 days on a grid, and room for a note.",
            "Tek dokunuşluk günlük kayıt, 90 günlük ızgara ve not için yer.",
            "Un registro diario de un toque, 90 días en cuadrícula y espacio para notas.",
        ),
        build=_mood,
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
