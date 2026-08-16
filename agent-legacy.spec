# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Agent-Legacy single-file binary.

Build:   pyinstaller agent-legacy.spec   (or just run build.bat)
Output:  dist\agent-legacy.exe  (console app — it is a terminal UI)
Ship:    agent-legacy.exe + a .env sidecar (secrets are NEVER baked in).
"""
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# Packages that load prompts / CSS / plugins / schemas dynamically —
# bundle all of their data files and hidden imports.
COLLECT_ALL = (
    "deepagents",
    "langgraph",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "textual",
    "rich",
    "httpx",
    "bs4",
    "markdownify",
    "aiosqlite",
    "dotenv",
)
for pkg in COLLECT_ALL:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Optional heavy dep pulled in by langchain-openai when present.
try:
    d, b, h = collect_all("tiktoken")
    datas += d
    binaries += b
    hiddenimports += h
except Exception:
    pass

hiddenimports += [
    "langgraph.checkpoint.sqlite",
    "langgraph.checkpoint.sqlite.aio",
    "agent",
    "agent.cli",
    "agent.commands",
    "agent.modes",
    "agent.router",
    "agent.config",
    "agent.cost",
    "agent.core.agent_factory",
    "agent.core.middlewares",
    "agent.memory.long_term",
    "agent.memory.memory_tools",
    "agent.prompts.houses",
    "agent.prompts.orchestrator",
    "agent.prompts.universal_ops",
    "agent.services.runner",
    "agent.services.speech",
    "agent.services.voice",
    "agent.tools.crawl",
    "agent.tools.registry",
    "agent.tools.route_llm",
    "agent.tools.web_search",
]

a = Analysis(
    ["agent/tapp.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy.testing"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="agent-legacy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # terminal UI — needs a real console
)
