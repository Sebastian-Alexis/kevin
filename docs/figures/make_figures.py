#!/usr/bin/env python3
"""Paper-style figures for KEVIN (docs/figures/*.png|svg).

Data: docs/bench-2026-10-24.md, docs/edge-cases-2026-10-24.md, README numbers, and the omp-gateway trace
(`journalctl --user -u omp-gateway -o cat | grep '^trace /v1/messages'` saved to --trace).
Logos are rasterized into --logos (anthropic, raspberrypi, apple, factory, moss, calhacks PNGs).

    /tmp/plotvenv/bin/python docs/figures/make_figures.py --trace /tmp/gw-trace.txt --logos /tmp/logos
"""
import argparse
import os
import textwrap

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.offsetbox import AnnotationBbox, OffsetImage  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

OUT = os.path.dirname(os.path.abspath(__file__))

# ---------- style ----------
for f in ("/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Regular.ttf",
          "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Medium.ttf",
          "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Bold.ttf"):
    if os.path.exists(f):
        font_manager.fontManager.addfont(f)
plt.rcParams.update({
    "font.family": ["Roboto", "DejaVu Sans"], "font.size": 10, "axes.titlesize": 12, "axes.titleweight": "bold",
    "axes.labelsize": 10, "axes.spines.top": False, "axes.spines.right": False,
    "axes.edgecolor": "#444", "axes.linewidth": 0.8, "xtick.color": "#333", "ytick.color": "#333",
    "axes.grid": True, "grid.color": "#e3e3e3", "grid.linewidth": 0.6, "axes.axisbelow": True,
    "legend.frameon": False, "legend.fontsize": 9, "figure.dpi": 110, "savefig.dpi": 300,
    "figure.facecolor": "white", "axes.facecolor": "white",
})
INK = "#1f2a37"
GREY = "#9aa4b1"
BLUE = "#2563eb"
ORANGE = "#ea7a1c"
GREEN = "#16a34a"
RED = "#dc2626"
PURPLE = "#7c3aed"
WATERMARK = "KEVIN  ·  KVM-Enabled Vision Interface for Native computer use  ·  Cal Hacks 13.0  ·  2026-10-24"


def logo(name, logos_dir, invert=False, bg_to_alpha=None):
    p = os.path.join(logos_dir, f"{name}.png")
    if not os.path.exists(p):
        return None
    im = Image.open(p).convert("RGBA")
    if bg_to_alpha is not None:  # key out a flat background colour
        arr = np.array(im).astype(int)
        d = np.abs(arr[..., :3] - np.array(bg_to_alpha)).sum(-1)
        arr[..., 3] = np.where(d < 40, 0, 255)
        im = Image.fromarray(arr.astype(np.uint8))
    if invert:
        rgb = ImageOps.invert(im.convert("RGB"))
        im = Image.merge("RGBA", (*rgb.split(), im.split()[3]))
    return im


def footer(fig, logos, caption=None, built_with=True):
    """Caption (left 68%), logo strip (right), watermark line. Figures reserve the bottom ~18% for this band."""
    fig_w, fig_h = fig.get_size_inches()
    fig.text(0.012, 0.012, WATERMARK, fontsize=7, color=GREY, ha="left", va="bottom")
    if caption:
        fig.text(0.012, 0.042, "\n".join(textwrap.wrap(caption, width=int(fig_w * 16.5 * 0.68))),
                 fontsize=8, color="#4b5563", ha="left", va="bottom", linespacing=1.35)
    if not built_with:
        return
    x = 0.988
    band_h = 0.26 / fig_h  # logo height in figure fraction (≈0.26 in)
    for name in ("calhacks", "moss", "factory", "anthropic", "raspberrypi"):
        im = logos.get(name)
        if im is None:
            continue
        w_px, h_px = im.size
        h = band_h * (1.35 if name == "calhacks" else 1.0)
        zoom = (h * fig_h * fig.dpi) / h_px
        w_frac = (w_px * zoom / fig.dpi) / fig_w
        fig.add_artist(AnnotationBbox(OffsetImage(im, zoom=zoom), (x - w_frac / 2, 0.052), xycoords="figure fraction",
                                      frameon=False, box_alignment=(0.5, 0.5)))
        x -= w_frac + 0.014
    fig.text(0.988, 0.052 + band_h * 0.95, "built with", fontsize=7, color=GREY, ha="right", va="bottom")


def save(fig, name):
    for ext in ("png", "svg"):
        fig.savefig(os.path.join(OUT, f"{name}.{ext}"), bbox_inches=None)
    plt.close(fig)
    print("wrote", name)


# ---------- data ----------
BENCH = [  # task, kind, first (turns, s), after (turns, s) or None, note
    ("web-lookup", "browser", (2, 16.9), None, ""),
    ("web-navigate", "browser", (3, 22.4), None, ""),
    ("hn-top3", "browser", (2, 16.9), None, ""),
    ("gmail-newest", "browser", (2, 15.8), None, ""),
    ("downloads-list", "native", (3, 11.6), None, ""),
    ("calculator", "native", (3, 17.1), None, ""),
    ("appearance", "native", (4, 25.5), None, ""),
    ("calendar-tomorrow", "native", (6, 30.1), None, ""),
    ("settings-version", "native", (6, 41.2), (5, 26.0), ""),
    ("multi-app", "native", (13, 52.8), (6, 42.9), "partial"),
    ("textedit", "native", (9, 62.0), (5, 23.4), "partial"),
    ("finder-folder", "native", (14, 81.0), (4, 20.3), ""),
]
EDGE = [  # case, turns, s, (rerun turns, s)
    ("drag-grow", 3, 14.7, None), ("dblclick", 4, 18.8, None), ("hidden-menubar", 3, 26.7, None),
    ("right-click", 4, 30.0, None), ("keyboard-nav", 5, 30.9, None), ("scroll-precise", 4, 31.7, None),
    ("slider", 7, 38.2, None), ("drag-shrink", 9, 58.6, None), ("textsel", 16, 65.4, None),
    ("modifier-click", 14, 86.9, (6, 21.5)),
]


def fig_bench(logos):
    fig = plt.figure(figsize=(9, 6.6))
    ax = fig.add_axes([0.19, 0.25, 0.77, 0.67])
    names = [b[0] for b in BENCH]
    y = np.arange(len(BENCH))[::-1]
    first = [b[2][1] for b in BENCH]
    after = [b[3][1] if b[3] else np.nan for b in BENCH]
    h = 0.38
    bars1 = ax.barh(y + h / 2, first, h, color=[GREY if b[1] == "browser" else INK for b in BENCH], label="first pass")
    ax.barh(y - h / 2, after, h, color=BLUE, label="after executor fixes + warm memory")
    for yi, b in zip(y, BENCH):
        t, s = b[2]
        ax.text(s + 1, yi + h / 2, f"{t} turns" + ("  ·  partial" if b[4] else ""), va="center", fontsize=8, color="#374151")
        if b[3]:
            ax.text(b[3][1] + 1, yi - h / 2, f"{b[3][0]} turns  (−{100 * (1 - b[3][1] / s):.0f}%)", va="center", fontsize=8, color=BLUE)
    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.set_xlabel("wall-clock seconds to task completion (lower is better)")
    ax.set_xlim(0, 100)
    ax.set_title("Task bench on a MacBook Air: 12 OSWorld-style tasks, all correct vs. ground truth")
    ax.axhline(y[3] - 0.5, color="#cbd5e1", lw=0.8, ls="--")
    ax.text(99, y[0] + 0.35, "browser tasks (2–3 turns, no fixes needed)", ha="right", fontsize=8.5, color="#6b7280", style="italic")
    ax.text(99, y[4] + 0.35, "native macOS apps (HID timing matters)", ha="right", fontsize=8.5, color="#6b7280", style="italic")
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=GREY, label="first pass, browser task"), Patch(color=INK, label="first pass, native app"),
                       Patch(color=BLUE, label="rerun after executor fixes, memory warm")], loc="center right", bbox_to_anchor=(1.0, 0.42))
    footer(fig, logos, "Native computer-tool loop (Opus 5.5 planner, Moss memory on). Correctness checked against GitHub API, sw_vers, "
                       "AppleInterfaceStyle, hw.model, Wikipedia infobox and the Downloads folder — not the model's own done flag. "
                       "Reruns followed three executor fixes (launcher pacing, settle before typing, Mac Delete→backspace).")
    save(fig, "fig-bench")


def fig_edge(logos):
    fig = plt.figure(figsize=(9, 5.4))
    ax = fig.add_axes([0.17, 0.26, 0.79, 0.64])
    y = np.arange(len(EDGE))[::-1]
    for yi, (name, t, s, rerun) in zip(y, EDGE):
        if rerun:
            ax.barh(yi, s, 0.62, color=RED, alpha=0.85)
            ax.barh(yi, rerun[1], 0.62, color=GREEN)
            ax.text(rerun[1] + 1, yi + 0.02, f"{rerun[0]} turns after the fix", va="center", fontsize=8, color="white", fontweight="medium")
            ax.text(s + 1, yi, f"{t} turns: cmd dropped on click", va="center", fontsize=8, color=RED)
        else:
            ax.barh(yi, s, 0.62, color=GREEN)
            ax.text(s + 1, yi, f"{t} turns", va="center", fontsize=8, color="#374151")
    ax.set_yticks(y)
    ax.set_yticklabels([e[0] for e in EDGE])
    ax.set_xlim(0, 120)
    ax.set_xlabel("wall-clock seconds (graded from saved HDMI frames)")
    ax.set_title("Executor edge cases: drags, sliders, text selection, scrolling, menus, modifier clicks — 10/10 pass")
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=GREEN, label="pass"), Patch(color=RED, label="first pass before the fix")], loc="upper right")
    footer(fig, logos, "Each case stresses the HID executor rather than the planner. Fixes shipped from this battery: hover→press→hold→frame-paced "
                       "drag with dwell and release; explicit start_coordinate; held mouse buttons; modifiers held across a click on the Pi; "
                       "zoom up to 4× after settle.")
    save(fig, "fig-edge-cases")


def fig_latency(logos, trace):
    rows = [l.split() for l in open(trace) if l.startswith("anthropic/")]
    lat = {m: np.array([float(r[1]) for r in rows if r[0] == m]) for m in set(r[0] for r in rows)}
    outs = {m: np.array([int(r[2]) for r in rows if r[0] == m]) for m in lat}
    son = "anthropic/claude-sonnet-5-5"
    v = lat[son]
    fig = plt.figure(figsize=(9, 5.6))
    ax = fig.add_axes([0.08, 0.32, 0.52, 0.57])
    ax2 = fig.add_axes([0.68, 0.32, 0.29, 0.57])
    clip = np.clip(v, 0, 12)
    ax.hist(clip, bins=np.arange(0, 12.5, 0.4), color=BLUE, alpha=0.9, edgecolor="white", lw=0.4)
    med, p90 = np.median(v), np.percentile(v, 90)
    ax.axvline(med, color=INK, lw=1.2)
    ax.axvline(p90, color=ORANGE, lw=1.2, ls="--")
    ax.text(med + 0.15, ax.get_ylim()[1] * 0.93, f"median {med:.1f} s", color=INK, fontsize=9)
    ax.text(p90 + 0.15, ax.get_ylim()[1] * 0.80, f"p90 {p90:.1f} s", color=ORANGE, fontsize=9)
    ax.set_xlabel("planner round-trip per step, seconds (screenshot in → action out; >12 s clipped)")
    ax.set_ylabel("steps")
    ax.set_title(f"Planner latency per step — Claude Sonnet 5.5, n = {len(v)} real steps")
    n_out = (v > 12).sum()
    ax.text(11.9, ax.get_ylim()[1] * 0.5, f"{n_out} steps > 12 s\n(upstream slowdowns,\nmax {v.max():.0f} s)", ha="right", fontsize=8, color="#6b7280")
    # right: latency vs output tokens, all models
    cols = {son: BLUE, "anthropic/claude-opus-5-5": PURPLE, "anthropic/claude-haiku-5-5": GREEN}
    labels = {son: "Sonnet 5.5", "anthropic/claude-opus-5-5": "Opus 5.5", "anthropic/claude-haiku-5-5": "Haiku 5.5"}
    for m in (son, "anthropic/claude-opus-5-5", "anthropic/claude-haiku-5-5"):
        if m in lat:
            ax2.scatter(outs[m], np.clip(lat[m], 0, 12), s=9, color=cols[m], alpha=0.55, label=f"{labels[m]} (n={len(lat[m])})", edgecolors="none")
    ax2.set_xlabel("output tokens per step")
    ax2.set_ylabel("seconds")
    ax2.set_ylim(0, 12)
    ax2.set_title("Latency scales with output", fontsize=10.5)
    ax2.legend(loc="upper left", fontsize=8, markerscale=1.8)
    footer(fig, logos, "Source: omp-gateway trace of every /v1/messages call made by the KEVIN loop and the Droid/Factory sessions on 2026-10-24 "
                       "(thinking off, image history trimmed to 3, max_tokens 1500). Short decisions (<150 output tokens) take ~2.1 s; "
                       "multi-action plans (≥300 tokens) ~3.9 s. The step time seen by the user adds ~0.3–1 s of HID action + HDMI settle.")
    save(fig, "fig-planner-latency")


def fig_memory(logos):
    fig = plt.figure(figsize=(11, 5.2))
    axa = fig.add_axes([0.06, 0.3, 0.4, 0.57])
    axb = fig.add_axes([0.54, 0.3, 0.43, 0.57])
    # (a) loop comparison on the same task
    tasks = ["Connections\ngrounded loop", "Connections\nnative loop", "HN top-3", "Weather", "Calendar\ntomorrow", "Login from\nlock screen"]
    secs = [60, 47, 9, 9, 14, 21]
    steps = ["15 steps", "9 turns", "2 turns", "2 turns", "3 turns", "5 turns"]
    cols = [GREY, BLUE, BLUE, BLUE, BLUE, BLUE]
    x = np.arange(len(tasks))
    axa.bar(x, secs, 0.62, color=cols)
    for xi, s, st in zip(x, secs, steps):
        axa.text(xi, s + 1.2, f"{s} s\n{st}", ha="center", fontsize=8, color="#374151")
    axa.set_xticks(x)
    axa.set_xticklabels(tasks, fontsize=7.8)
    axa.set_ylabel("seconds, end to end")
    axa.set_ylim(0, 75)
    axa.set_title("(a) Native computer-tool loop vs. grounded planner", fontsize=10.5)
    # (b) cold vs warm memory
    pairs = [("Factory MCP task", (8, 34), (5, 20)), ("settings-version", (6, 41.2), (5, 26.0)), ("textedit", (9, 62.0), (5, 23.4)),
             ("multi-app", (13, 52.8), (6, 42.9)), ("finder-folder", (14, 81.0), (4, 20.3))]
    x = np.arange(len(pairs))
    w = 0.36
    axb.bar(x - w / 2, [p[1][1] for p in pairs], w, color=GREY, label="cold: machine unknown, no notes")
    axb.bar(x + w / 2, [p[2][1] for p in pairs], w, color=ORANGE, label="warm: Moss playbook recalled")
    for xi, p in zip(x, pairs):
        axb.text(xi - w / 2, p[1][1] + 1.2, f"{p[1][0]}t", ha="center", fontsize=8, color="#374151")
        axb.text(xi + w / 2, p[2][1] + 1.2, f"{p[2][0]}t", ha="center", fontsize=8, color=ORANGE)
    axb.set_xticks(x)
    axb.set_xticklabels([p[0] for p in pairs], fontsize=8)
    axb.set_ylim(0, 95)
    axb.set_ylabel("seconds")
    axb.set_title("(b) Memory keyed to the machine: cold vs. warm", fontsize=10.5)
    axb.legend(loc="upper left", fontsize=8)
    footer(fig, logos, "(a) Same Connections puzzle: grounded loop (plan → Holo4 grounding → verify) vs. the native Anthropic computer tool loop; "
                       "the other bars are native-loop runs from the KVM. (b) Second visit to a known Mac: success-only notes recalled from Moss "
                       "(index ai-kvm-playbook) cut turns 35–70%. t = planner turns.")
    save(fig, "fig-loop-and-memory")


def fig_judge(logos):
    fig = plt.figure(figsize=(9, 5.0))
    ax = fig.add_axes([0.08, 0.3, 0.4, 0.55])
    ax2 = fig.add_axes([0.58, 0.3, 0.38, 0.55])
    labels = ["step-outcome judge\n(expect, n=20)", "Enter gate\n(typed where wanted, n=8)"]
    q = [19 / 20 * 100, 8 / 8 * 100]
    h = [19 / 20 * 100, 4 / 8 * 100]
    x = np.arange(2)
    w = 0.36
    ax.bar(x - w / 2, q, w, color=BLUE, label="Qwen 2.7 (local vLLM)")
    ax.bar(x + w / 2, h, w, color=PURPLE, label="Holo4 (local vLLM)")
    for xi, a, b in zip(x, q, h):
        ax.text(xi - w / 2, a + 1.5, f"{a:.0f}%", ha="center", fontsize=9)
        ax.text(xi + w / 2, b + 1.5, f"{b:.0f}%", ha="center", fontsize=9)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8.5)
    ax.set_ylim(0, 125)
    ax.set_ylabel("accuracy vs. hand labels, %")
    ax.set_title("(a) Local judge accuracy", fontsize=10.5)
    ax.legend(loc="upper center", fontsize=8, ncol=2, bbox_to_anchor=(0.5, 1.0))
    ql = [0.7, 1.1]
    hl = [0.3, 0.4]
    ax2.bar(x - w / 2, ql, w, color=BLUE)
    ax2.bar(x + w / 2, hl, w, color=PURPLE)
    for xi, a, b in zip(x, ql, hl):
        ax2.text(xi - w / 2, a + 0.03, f"{a} s", ha="center", fontsize=9)
        ax2.text(xi + w / 2, b + 0.03, f"{b} s", ha="center", fontsize=9)
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, fontsize=8.5)
    ax2.set_ylim(0, 1.4)
    ax2.set_ylabel("seconds per judgement (GB10, local)")
    ax2.set_title("(b) Judge latency", fontsize=10.5)
    footer(fig, logos, "The Enter gate runs before every Return the planner sends while text is pending: a local VLM confirms the text landed in the "
                       "field the task wanted. Qwen 2.7 is the gate/verifier/lesson-writer in production; Holo4 is kept as the grounder only.")
    save(fig, "fig-judge")


def fig_integration(logos):
    fig = plt.figure(figsize=(9, 4.9))
    ax = fig.add_axes([0.08, 0.31, 0.4, 0.53])
    ax2 = fig.add_axes([0.57, 0.31, 0.39, 0.53])
    x = np.arange(3)
    calls = [2, 6.5, 1]
    ax.bar(x, calls, 0.58, color=[BLUE, GREY, ORANGE])
    for xi, c, lab in zip(x, calls, ["2", "6–7", "1 (+ loop inside KEVIN)"]):
        ax.text(xi, c + 0.15, lab, ha="center", fontsize=8.5)
    ax.set_xticks(x)
    ax.set_xticklabels(["direct loop\n(python -m agent)", "Droid app driving\nthe computer tool", "Droid app calling\nrun(task) over MCP"], fontsize=8)
    ax.set_ylabel("planner calls per short task")
    ax.set_ylim(0, 8.5)
    ax.set_title("(a) Where the planner runs matters", fontsize=10.5)
    x = np.arange(3)
    secs = [9.0, 34.0, 9.5]
    ax2.bar(x, secs, 0.58, color=[BLUE, GREY, ORANGE])
    for xi, s in zip(x, secs):
        ax2.text(xi, s + 0.6, f"{s:g} s", ha="center", fontsize=8.5)
    ax2.set_xticks(x)
    ax2.set_xticklabels(["direct loop\nHN top-3", "Droid app, step-by-step\n(cold memory)", "Droid app → run(task)\nHN top-3 over MCP"], fontsize=8)
    ax2.set_ylabel("seconds end to end")
    ax2.set_ylim(0, 40)
    ax2.set_title("(b) Same task, three ways in", fontsize=10.5)
    footer(fig, logos, "Factory/Droid sessions add ~5 model calls when they drive the computer tool step by step (tool-schema and script-wrapper overhead). "
                       "Moving the loop onto the KVM behind one MCP tool, run(task), returns the whole task with streamed progress at direct-loop speed.")
    save(fig, "fig-integration")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", default="/tmp/gw-trace.txt")
    ap.add_argument("--logos", default=os.path.join(OUT, "logos"))
    a = ap.parse_args()
    logos = {
        "calhacks": logo("calhacks", a.logos),
        "moss": logo("moss-avatar", a.logos, bg_to_alpha=(240, 240, 240)),
        "factory": logo("factory", a.logos, invert=True, bg_to_alpha=None),
        "anthropic": logo("anthropic", a.logos),
        "raspberrypi": logo("raspberrypi", a.logos),
    }
    if logos["factory"] is not None:  # white mark on black → black mark, transparent
        arr = np.array(Image.open(os.path.join(a.logos, "factory.png")).convert("L"))
        rgba = np.zeros((*arr.shape, 4), np.uint8)
        rgba[..., 3] = arr
        logos["factory"] = Image.fromarray(rgba)
    fig_bench(logos)
    fig_edge(logos)
    if os.path.exists(a.trace):
        fig_latency(logos, a.trace)
    fig_memory(logos)
    fig_judge(logos)
    fig_integration(logos)


if __name__ == "__main__":
    main()
