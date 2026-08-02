from __future__ import annotations

import re
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent / "config" / "normalise.yaml"


class Normaliser:
    def __init__(self) -> None:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        self.version: int = cfg["version"]
        self.prefixes: list[str] = [p.upper() for p in cfg["strip_prefixes"]]
        self.truncate: int = cfg["truncate"]
        states = "|".join(cfg["strip_states"])
        countries = "|".join(cfg["strip_countries"])
        self._trailing = re.compile(rf"\s+(?:{states})\s*(?:{countries})?$|\s+(?:{countries})$")

    def apply(self, raw: str) -> str:
        s = " ".join(raw.upper().split())
        stripped = True
        while stripped:
            stripped = False
            for p in self.prefixes:
                if s.startswith(p):
                    rest = s[len(p):]
                    if p.endswith("*") or rest == "" or rest[0] in " *":
                        s = rest.lstrip(" *")
                        stripped = True
        s = re.sub(r"\bXX\d+\b", " ", s)                     # card fragments
        s = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", s)  # dates
        s = re.sub(r"\b\w*\d{4,}\w*\b", " ", s)              # refs, receipts, long numbers
        s = " ".join(s.split())
        while True:
            t = self._trailing.sub("", s)
            if t == s:
                break
            s = t
        s = s.strip(" -*#.,/\\")
        return " ".join(s.split())[: self.truncate]
