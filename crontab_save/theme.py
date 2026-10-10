"""Оформление «Терминал»: зелёный на чёрном, моноширинный шрифт, прямые углы.

Вся тема - одна таблица стилей GTK CSS, поэтому цвета меняются в одном месте
(словарь PALETTE ниже).
"""

from __future__ import annotations

PALETTE = {
    "bg": "#0c0f0c",        # фон окна и полей
    "panel": "#101610",     # чуть светлее: заголовки таблиц, всплывающие меню
    "fg": "#8cff98",        # основной текст и акцент
    "dim": "#4fae5a",       # подписи и подсказки
    "line": "#2a6b32",      # рамки
    "hover": "#14331a",     # наведение и выделение
    "on_fg": "#0c0f0c",     # текст на залитой акцентом кнопке
    "err": "#ff6b6b",
    "warn_bg": "#2a2200",
    "warn_fg": "#ffd166",
}

FONT = '"JetBrains Mono", "DejaVu Sans Mono", "Liberation Mono", "Adwaita Mono", monospace'

_TEMPLATE = """
* {{ font-family: {font}; font-size: 10pt; border-radius: 0; box-shadow: none; text-shadow: none;
     -gtk-icon-shadow: none; }}
window, dialog, messagedialog, .background {{ background-color: {bg}; color: {fg}; }}
label {{ color: {fg}; }}
.hint, .dim-label {{ color: {dim}; }}
.section {{ color: {fg}; font-weight: bold; }}
.app-title {{ font-size: 17pt; font-weight: bold; color: {fg}; }}
.ok {{ color: {fg}; font-weight: bold; }}
.err {{ color: {err}; }}
.banner {{ background-color: {warn_bg}; color: {warn_fg}; border: 1px solid {warn_fg}; padding: 8px 12px; }}
.banner label {{ color: {warn_fg}; }}
.mono {{ font-size: 9pt; }}

entry, spinbutton, textview, textview text, treeview.view {{
    background-color: {bg}; color: {fg}; caret-color: {fg}; background-image: none; }}
entry, spinbutton {{ border: 1px solid {line}; padding: 4px 6px; min-height: 22px; }}
textview, textview text, treeview.view {{ border: 0; }}  /* рамку рисует scrolledwindow */
spinbutton {{ padding: 0; }}
spinbutton text, spinbutton entry {{ border: 0; background-color: transparent; padding: 4px 6px; }}
entry:focus, spinbutton:focus {{ border-color: {fg}; }}
entry:disabled, textview:disabled, textview:disabled text {{ color: {dim}; background-color: {panel}; }}
textview text selection, entry selection, treeview.view:selected {{ background-color: {line}; color: {fg}; }}
spinbutton button {{ background-image: none; background-color: {bg}; color: {fg};
                     border: 0; border-left: 1px solid {line}; }}
spinbutton button:hover {{ background-color: {hover}; }}

button {{ background-image: none; background-color: transparent; color: {fg};
          border: 1px solid {line}; padding: 5px 12px; min-height: 22px; }}
button:hover {{ background-color: {hover}; border-color: {fg}; }}
button:active {{ background-color: {line}; }}
button:disabled {{ color: {dim}; border-color: {panel}; }}
button.suggested-action {{ background-color: {fg}; color: {on_fg}; border-color: {fg}; font-weight: bold; }}
button.suggested-action:hover {{ background-color: {dim}; border-color: {dim}; }}
button.suggested-action:disabled {{ background-color: {line}; color: {bg}; }}
button label {{ color: inherit; }}

combobox button.combo {{ padding: 4px 8px; }}
menu, .popup, popover, popover.background {{ background-color: {panel}; color: {fg}; border: 1px solid {line}; }}
menuitem, modelbutton {{ color: {fg}; padding: 4px 8px; }}
menuitem:hover, modelbutton:hover {{ background-color: {hover}; }}
menuitem label, modelbutton label {{ color: {fg}; }}

radio, check {{ background-image: none; background-color: {bg}; border: 1px solid {fg}; color: {on_fg};
                -gtk-icon-source: none; min-width: 14px; min-height: 14px; }}
radio:checked, check:checked {{ background-color: {fg}; }}
radio {{ border-radius: 50%; }}
radiobutton label {{ color: {fg}; }}

notebook {{ background-color: {bg}; }}
notebook > header {{ background-color: {bg}; border-color: {line}; box-shadow: none; }}
notebook > header > tabs {{ margin: 0; }}
notebook > header > tabs > tab {{ background-color: {bg}; color: {dim}; padding: 6px 14px; margin: 0;
                                  min-width: 0; border: 0; border-bottom: 2px solid transparent;
                                  box-shadow: none; outline: none; }}
notebook > header > tabs > tab:hover {{ color: {fg}; }}
notebook > header > tabs > tab:checked {{ color: {fg}; border-bottom-color: {fg}; font-weight: bold; }}
notebook > stack {{ background-color: {bg}; }}

scrolledwindow, scrolledwindow viewport, viewport {{ background-color: {bg}; }}
scrolledwindow.frame, frame > border {{ border: 1px solid {line}; }}
scrollbar {{ background-color: {bg}; border: 0; }}
scrollbar slider {{ background-color: {line}; min-width: 8px; min-height: 8px; border: 0; }}
scrollbar slider:hover {{ background-color: {dim}; }}

treeview.view {{ background-color: {bg}; }}
treeview.view:hover {{ background-color: {hover}; }}
treeview header button {{ background-color: {panel}; color: {dim}; border: 0; border-bottom: 1px solid {line};
                          border-right: 1px solid {line}; font-weight: bold; }}

separator {{ background-color: {line}; min-height: 1px; min-width: 1px; }}
progressbar trough {{ background-color: {panel}; border: 1px solid {line}; min-height: 8px; }}
progressbar progress {{ background-color: {fg}; background-image: none; border: 0; min-height: 8px; }}
flowboxchild {{ padding: 0; }}
tooltip, tooltip.background {{ background-color: {panel}; color: {fg}; border: 1px solid {line}; }}
"""


def build_css() -> bytes:
    return _TEMPLATE.format(font=FONT, **PALETTE).encode("utf-8")
