#!/usr/bin/env python3
"""Convertit un catalogue BattleScribe (wh40k 8e) en JSON compact pour l'appli.

Usage : python3 -I convert.py <dossier_repo> "<Catalogue sans .cat>" <sortie.json>
"""
import json
import re
import sys
import xml.etree.ElementTree as ET

CP_ID = "2d3b-b544-ad49-fb75"
SLOT_ORDER = ["HQ", "Troops", "Elites", "Fast Attack", "Heavy Support",
              "Flyer", "Dedicated Transport", "Lord of War", "Fortification"]
# Options de campagne (Crusade) : jamais utilisées en parties à points
SKIP_NAMES = {"Battle Honours", "Has Battle Honours", "Battle Scars", "Has Battle Scars"}
SELECTORS = ["Dynastic Code", "Legion", "Chaos Allegiance", "Craftworld Attribute", "Clan Kultur",
             "T'au Empire Sept Choice", "Hive Fleet", "Forge World Choice", "Household Choice",
             "**Chapter Selection**", "**CHAPTER**", "Regimental Doctrine", "Order Convictions", "Cult Creed",
             "Masque Form", "Cults of the Legion"]
SLOT_NAMES = set(SLOT_ORDER)  # appartenance seulement : l'ordre vient de SLOT_ORDER


def tag(e):
    return e.tag.split("}")[1]


def children(e, name):
    sub = e.find("{*}" + name)
    return list(sub) if sub is not None else []


class Repo:
    """Tous les fichiers du jeu, chargés une fois : l'index des ids est commun (liens entre catalogues)."""

    def __init__(self, folder):
        import glob
        import os
        self.gst = ET.parse(f"{folder}/Warhammer 40,000 8th Edition.gst").getroot()
        self.cats = {}      # nom de fichier sans .cat -> racine
        self.catid = {}     # id du catalogue -> racine
        self.byid = {}
        for e in self.gst.iter():
            if e.get("id"):
                self.byid[e.get("id")] = e
        for path in sorted(glob.glob(f"{folder}/*.cat")):
            root = ET.parse(path).getroot()
            self.cats[os.path.basename(path)[:-4]] = root
            self.catid[root.get("id")] = root
            for e in root.iter():
                if e.get("id"):
                    self.byid.setdefault(e.get("id"), e)


class Converter:
    def __init__(self, repo, catalogue):
        if not isinstance(repo, Repo):
            repo = Repo(repo)
        self.repo = repo
        self.gst = repo.gst
        self.cat = repo.cats[catalogue]
        self.byid = repo.byid
        self.warnings = set()

    # ---------- utilitaires ----------
    def field(self, f):
        if f == "points":
            return "pts"
        if f == CP_ID:
            return "cp"
        return f

    def costs(self, e):
        out = {}
        for c in children(e, "costs"):
            n = c.get("name", "").strip().lower()
            key = {"pts": "pts", "cp": "cp"}.get(n)  # PL volontairement ignorés
            if key:
                v = float(c.get("value"))
                out[key] = out.get(key, 0) + v
        return out

    def cond(self, c):
        t = tag(c)
        if t == "condition":
            return {"ty": c.get("type"), "v": float(c.get("value")), "f": c.get("field"),
                    "s": c.get("scope"), "c": c.get("childId"),
                    "i": c.get("includeChildSelections") == "true"}
        if t == "conditionGroup":
            parts = []
            for x in c:
                if tag(x) in ("condition", "conditionGroup"):
                    parts.append(self.cond(x))
                elif tag(x) in ("conditions", "conditionGroups"):  # conteneurs imbriqués (export New Recruit)
                    parts.extend(self.cond(y) for y in x if tag(y) in ("condition", "conditionGroup"))
            return {"g": c.get("type", "and"), "p": parts}
        return None

    def mod_conditions(self, m):
        parts = []
        for k in ("conditions", "conditionGroups"):
            for x in children(m, k):
                parts.append(self.cond(x))
        return parts

    def mods(self, e, also=None):
        out = []
        groups = children(e, "modifierGroups")
        if groups:
            self.warnings.add("modifierGroups ignorés")
        # modificateurs qui changent un min/max : le champ est l'id d'une contrainte
        cmap = {}
        for src in (e, also):
            if src is None:
                continue
            for c in children(src, "constraints"):
                if c.get("field") == "selections" and c.get("scope") == "parent" and c.get("type") in ("min", "max"):
                    cmap[c.get("id")] = c.get("type")
        for m in children(e, "modifiers"):
            f = cmap.get(m.get("field")) or self.field(m.get("field"))
            item = {"t": m.get("type"), "f": f, "v": m.get("value")}
            conds = self.mod_conditions(m)
            if conds:
                item["c"] = conds
            reps = []
            for r in children(m, "repeats"):
                reps.append({"f": r.get("field"), "s": r.get("scope"), "c": r.get("childId"),
                             "n": float(r.get("repeats", "1")), "v": float(r.get("value", "1")),
                             "i": r.get("includeChildSelections") == "true"})
            if reps:
                item["r"] = reps
            # on ne garde que ce que le moteur sait appliquer
            if f in ("pts", "cp", "hidden", "name", "min", "max"):  # pas de PL
                out.append(item)
        return out

    def constraints(self, e):
        mn = mx = None
        extra = []
        for c in children(e, "constraints"):
            if c.get("field") != "selections":
                continue
            v = float(c.get("value"))
            if c.get("scope") == "parent":
                if c.get("type") == "min":
                    mn = v if mn is None else max(mn, v)
                elif c.get("type") == "max":
                    mx = v if mx is None else min(mx, v)
            elif c.get("scope") in ("roster", "force"):
                extra.append({"ty": c.get("type"), "v": v, "s": c.get("scope")})
        return mn, mx, extra

    def profile(self, p):
        ptype = None
        t = self.byid.get(p.get("typeId"))
        ptype = t.get("name") if t is not None else p.get("typeName")
        chars = [[c.get("name"), (c.text or "").strip()] for c in children(p, "characteristics")]
        return {"type": ptype, "name": p.get("name"), "chars": chars}

    def rule_text(self, r):
        d = r.find("{*}description")
        return (d.text or "").strip() if d is not None else ""

    def info(self, e):
        profiles, rules = [], []
        for p in children(e, "profiles"):
            profiles.append(self.profile(p))
        for r in children(e, "rules"):
            rules.append({"name": r.get("name"), "text": self.rule_text(r)})
        for il in children(e, "infoLinks"):
            t = self.byid.get(il.get("targetId"))
            if t is None:
                continue
            if tag(t) == "profile":
                profiles.append(self.profile(t))
            elif tag(t) == "rule":
                rules.append({"name": t.get("name"), "text": self.rule_text(t)})
        return profiles, rules

    def categories(self, *entries):
        names, primary = [], None
        for e in entries:
            for cl in children(e, "categoryLinks"):
                t = self.byid.get(cl.get("targetId"))
                if t is None:
                    continue
                names.append(t.get("name"))
                if cl.get("primary") == "true":
                    primary = t.get("name")
        return names, primary

    # ---------- noeud ----------
    def node(self, entry, link=None, path=()):
        """entry = selectionEntry ou selectionEntryGroup ; link = entryLink éventuel."""
        eid = entry.get("id")
        if eid in path:
            return None
        path = path + (eid,)
        is_group = tag(entry) == "selectionEntryGroup"
        name = (link.get("name") if link is not None and link.get("name") else None) or entry.get("name")
        mn, mx, extra = self.constraints(entry)
        if link is not None:
            lmn, lmx, lextra = self.constraints(link)
            mn = lmn if lmn is not None else mn
            mx = lmx if lmx is not None else mx
            extra += lextra
        n = {"id": eid, "name": name, "type": "group" if is_group else entry.get("type")}
        if link is not None:
            n["link"] = link.get("id")
        hidden = entry.get("hidden") == "true" or (link is not None and link.get("hidden") == "true")
        if hidden:
            n["hidden"] = True
        if mn is not None:
            n["min"] = mn
        if mx is not None:
            n["max"] = mx
        if extra:
            n["lim"] = extra
        cost = self.costs(entry)
        if link is not None:
            for k, v in self.costs(link).items():
                cost[k] = cost.get(k, 0) + v
        cost = {k: v for k, v in cost.items() if v}
        if cost:
            n["cost"] = cost
        mods = self.mods(entry, link) + (self.mods(link, entry) if link is not None else [])
        if mods:
            n["mods"] = mods
        profiles, rules = self.info(entry)
        if link is not None:
            p2, r2 = self.info(link)
            profiles += p2
            rules += r2
        if profiles:
            n["profiles"] = profiles
        if rules:
            n["rules"] = rules
        cids = []
        for src in (entry, link):
            if src is None:
                continue
            for cl in children(src, "categoryLinks"):
                if cl.get("targetId") and cl.get("targetId") not in cids:
                    cids.append(cl.get("targetId"))
        if cids and not is_group:
            n["cids"] = cids  # catégories de l'entrée : certaines conditions les comptent (ex. catégorie « Warlord »)
        if is_group and entry.get("defaultSelectionEntryId"):
            n["default"] = entry.get("defaultSelectionEntryId")
        kids = []
        for sub in children(entry, "selectionEntries"):
            if sub.get("name") in SKIP_NAMES:
                continue
            k = self.node(sub, None, path)
            if k:
                kids.append(k)
        for sub in children(entry, "selectionEntryGroups"):
            if sub.get("name") in SKIP_NAMES:
                continue
            k = self.node(sub, None, path)
            if k:
                kids.append(k)
        for l in children(entry, "entryLinks"):
            t = self.byid.get(l.get("targetId"))
            if t is None or (l.get("name") or t.get("name")) in SKIP_NAMES:
                continue
            k = self.node(t, l, path)
            if k:
                kids.append(k)
        if kids:
            n["kids"] = kids
        return n

    # ---------- détachements ----------
    def detachments(self):
        out = []
        for f in children(self.gst, "forceEntries"):
            name = f.get("name")
            m = re.search(r"([+-]\s?\d+)\s?CP", name)
            cp = int(m.group(1).replace(" ", "")) if m else 0
            slots = {}
            for cl in children(f, "categoryLinks"):
                t = self.byid.get(cl.get("targetId"))
                if t is None or t.get("name") not in SLOT_NAMES:
                    continue
                mn, mx = 0, None
                for c in cl.iter():
                    if tag(c) == "constraint" and c.get("scope") == "force" and c.get("field") == "selections":
                        if c.get("type") == "min":
                            mn = int(float(c.get("value")))
                        elif c.get("type") == "max":
                            mx = int(float(c.get("value")))
                slots[t.get("name")] = [mn, mx]
            clean = re.sub(r"\s*[+-]\s?\d+\s?CP", "", name).strip()
            out.append({"id": f.get("id"), "name": clean, "cp": cp, "slots": slots})
        return out

    # ---------- unités racine ----------
    def roots(self):
        units = []
        seen = set()
        for src in self.sources():
            pairs = [(l, self.byid.get(l.get("targetId"))) for l in children(src, "entryLinks")]
            pairs += [(None, e) for e in children(src, "selectionEntries")]
            for l, t in pairs:
                if t is None or tag(t) != "selectionEntry":
                    continue
                if t.get("type") not in ("unit", "model"):
                    continue
                names, primary = self.categories(*(x for x in (l, t) if x is not None))
                if primary not in SLOT_NAMES:
                    continue
                key = (l.get("name") if l is not None and l.get("name") else t.get("name"), t.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                n = self.node(t, l)
                if not n:
                    continue
                n["slot"] = primary
                n["cats"] = [c for c in names if c not in SLOT_NAMES]
                units.append(n)
        units.sort(key=lambda u: (SLOT_ORDER.index(u["slot"]) if u["slot"] in SLOT_ORDER else 99, u["name"], u["id"]))
        return units

    def sources(self):
        """Catalogue principal + catalogues liés dont les entrées racine sont importées."""
        out, todo, seen = [], [self.cat], set()
        while todo:
            c = todo.pop(0)
            if c.get("id") in seen:
                continue
            seen.add(c.get("id"))
            out.append(c)
            for cl in children(c, "catalogueLinks"):
                if cl.get("importRootEntries") == "true":
                    t = self.repo.catid.get(cl.get("targetId"))
                    if t is not None:
                        todo.append(t)
        return out

    def selector(self, catalogue):
        """Choix de sous-faction (dynastie, légion, chapitre...) : entrée racine d'amélioration, prise dans la liste SELECTORS."""
        for want in SELECTORS:
            for src in self.sources():
                for l in children(src, "entryLinks"):
                    t = self.byid.get(l.get("targetId"))
                    if t is None or (l.get("name") or t.get("name")) != want:
                        continue
                    if tag(t) not in ("selectionEntry", "selectionEntryGroup") or t.get("type") in ("unit", "model"):
                        continue
                    n = self.node(t, l)
                    if not n:
                        continue
                    if n["type"] == "group":  # enveloppe : le moteur attend une entrée contenant un groupe
                        n = {"id": n["id"] + "-sel", "name": n["name"], "type": "upgrade", "kids": [n]}
                    return {"label": want.strip("*").strip(), "node": n}
        return None

    def convert(self):
        return {
            "name": self.cat.get("name"),
            "revision": self.cat.get("revision"),
            "detachments": self.detachments(),
            "units": self.roots(),
            "selector": self.selector(self.cat.get("name")),
        }


def apply_overrides(data, path):
    """Corrige des points : [{unit, entry, pts, includedFree}] ; l'équipement imposé passe à 0 si includedFree."""
    import os
    if not os.path.exists(path):
        return 0
    n = 0
    for o in json.load(open(path, encoding="utf-8")):
        for u in data["units"]:
            if u["name"] != o["unit"]:
                continue
            stack = list(u.get("kids", []))
            while stack:
                k = stack.pop()
                stack.extend(k.get("kids", []))
                if k["name"] != o["entry"]:
                    continue
                k.setdefault("cost", {})["pts"] = o["pts"]
                k["mods"] = [m for m in k.get("mods", []) if m["f"] != "pts"]
                if o.get("includedFree"):
                    for c in k.get("kids", []):
                        if c.get("min", 0) > 0 and c["type"] != "group":
                            c["cost"] = {x: v for x, v in c.get("cost", {}).items() if x != "pts"}
                            c["mods"] = [m for m in c.get("mods", []) if m["f"] != "pts"]
                n += 1
    return n


RM = "[text removed]"
# rangement dans le sélecteur de factions (nom du catalogue -> groupe)
GROUP_OVERRIDE = {"Tyranids - Genestealer Cults": "Xenos", "Fallen": "Chaos"}


class OldTexts:
    """Textes (règles, capacités, effets d'armes) de l'ancien dépôt, bons, à remettre à la place de « [text removed] »."""

    def __init__(self, folder):
        import collections
        self.local = collections.defaultdict(dict)    # fichier -> {(type, nom): {caractéristique: texte}}
        self.glob = collections.defaultdict(collections.Counter)  # (type, nom) -> Counter(tuple(chars))
        self.lrules = collections.defaultdict(dict)   # fichier -> {nom: texte}
        self.grules = collections.defaultdict(collections.Counter)
        repo = Repo(folder)
        first = next(iter(repo.cats))
        sources = list(repo.cats.items()) + [("__gst__", repo.gst)]  # le gst contient aussi des textes (traits de seigneur de guerre du livre de base, etc.)
        for fname, root in sources:
            conv = Converter(repo, first if fname == "__gst__" else fname)
            for e in root.iter():
                t = tag(e)
                if t == "profile":
                    p = conv.profile(e)
                    key = (p["type"], p["name"])
                    chars = {c[0]: c[1] for c in p["chars"] if c[1] and c[1] != RM}
                    if chars:
                        self.local[fname].setdefault(key, chars)
                        self.glob[key][tuple(sorted(chars.items()))] += 1
                elif t == "rule":
                    d = e.find("{*}description")
                    txt = (d.text or "").strip() if d is not None else ""
                    if txt and txt != RM:
                        self.lrules[fname].setdefault(e.get("name"), txt)
                        self.grules[e.get("name")][txt] += 1

    def prof(self, fname, ptype, name):
        r = self.local.get(fname, {}).get((ptype, name))
        if r:
            return r
        c = self.glob.get((ptype, name))
        return dict(c.most_common(1)[0][0]) if c else None

    def rule(self, fname, name):
        r = self.lrules.get(fname, {}).get(name)
        if r:
            return r
        c = self.grules.get(name)
        return c.most_common(1)[0][0] if c else None


def restore_texts(data, fname, old, user, stats):
    """Remplace les « [text removed] » par les textes de l'ancien dépôt, puis applique le fichier de corrections de l'utilisateur."""
    def fix_profile(p):
        key = p["type"] + "|" + p["name"]
        src = old.prof(fname, p["type"], p["name"]) if old else None
        for c in p["chars"]:
            if c[1] != RM:
                continue
            u = user.get(key + "|" + c[0])
            if u is not None:
                c[1] = u
                stats["user"] += 1
            elif src and src.get(c[0]):
                c[1] = src[c[0]]
                stats["old"] += 1
            else:
                stats["missing"].add(key + "|" + c[0])

    def fix_rule(r):
        u = user.get("Rule|" + r["name"])
        if r["text"] == RM:
            if u is not None:
                r["text"] = u
                stats["user"] += 1
            else:
                o = old.rule(fname, r["name"]) if old else None
                if o:
                    r["text"] = o
                    stats["old"] += 1
                else:
                    stats["missing"].add("Rule|" + r["name"])

    def walk(n):
        for p in n.get("profiles", []):
            fix_profile(p)
        for r in n.get("rules", []):
            fix_rule(r)
        for k in n.get("kids", []):
            walk(k)
    for u in data["units"]:
        walk(u)
    if data.get("selector"):
        walk(data["selector"]["node"])
    for r in data.get("rules", []):
        fix_rule(r)


def faction_rules(data):
    """Règles communes à la faction : celles portées par au moins 40 % des unités."""
    import collections
    count = collections.Counter()
    sample = {}
    for u in data["units"]:
        seen = set()
        stack = [u]
        while stack:
            n = stack.pop()
            stack.extend(n.get("kids", []))
            for r in n.get("rules", []):
                seen.add(r["name"]); sample.setdefault(r["name"], r["text"])
        for nme in seen:
            count[nme] += 1
    lim = max(3, int(len(data["units"]) * 0.4))
    return [{"name": nme, "text": sample[nme]} for nme, c in count.most_common() if c >= lim and not nme.endswith("(Instructions)")][:6]


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def convert_all(folder, outdir, oldfolder=None):
    """Tous les catalogues jouables -> outdir/<slug>.json + outdir/index.json."""
    import os
    os.makedirs(outdir, exist_ok=True)
    repo = Repo(folder)
    ovdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overrides")
    index = []
    old = OldTexts(oldfolder) if oldfolder else None
    usrp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "textes", "textes.json")
    user = json.load(open(usrp, encoding="utf-8")) if os.path.exists(usrp) else {}
    stats = {"old": 0, "user": 0, "missing": set()}
    for fname, root in repo.cats.items():
        if root.get("library") == "true":
            continue
        conv = Converter(repo, fname)
        data = conv.convert()
        if not data["units"]:
            continue
        apply_overrides(data, os.path.join(ovdir, fname + ".json"))
        data["rules"] = faction_rules(data)
        restore_texts(data, fname, old, user, stats)
        slug = slugify(fname)
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(outdir, slug + ".json"), "w", encoding="utf-8") as fh:
            fh.write(raw)
        name = data["name"]
        group = GROUP_OVERRIDE.get(name) or (name.split(" - ")[0] if " - " in name else "Xenos")
        index.append({"id": slug, "name": name.split(" - ")[-1], "group": group,
                      "units": len(data["units"]), "rev": data["revision"], "kb": len(raw.encode("utf-8")) // 1000})
    import hashlib
    h = hashlib.md5()
    for f in sorted(os.listdir(outdir)):
        if f.endswith(".json") and f != "index.json":
            h.update(open(os.path.join(outdir, f), "rb").read())
    build = h.hexdigest()[:10]
    index.sort(key=lambda x: (x["group"] == "Unaligned", x["group"], x["name"]))
    print("textes restaurés :", stats["old"], "| corrigés à la main :", stats["user"], "| manquants :", len(stats["missing"]))
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "textes"), exist_ok=True)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "textes", "a-completer.json"), "w", encoding="utf-8") as fh:
        json.dump({k: RM for k in sorted(stats["missing"])}, fh, ensure_ascii=False, indent=0)
    with open(os.path.join(outdir, "index.json"), "w", encoding="utf-8") as fh:
        json.dump({"system": repo.gst.get("name"), "rev": repo.gst.get("revision"), "build": build, "factions": index}, fh, ensure_ascii=False, separators=(",", ":"))
    print(len(index), "factions ->", outdir)


if __name__ == "__main__":
    import os
    if sys.argv[1] == "--all":
        convert_all(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
        sys.exit(0)
    repo, catalogue, dest = sys.argv[1:4]
    conv = Converter(repo, catalogue)
    data = conv.convert()
    applied = apply_overrides(data, os.path.join(os.path.dirname(os.path.abspath(__file__)), "overrides", catalogue + ".json"))
    if applied:
        print(f"{applied} correction(s) de points appliquée(s)")
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"{data['name']} rev {data['revision']} : {len(data['units'])} unités, "
          f"{len(data['detachments'])} détachements -> {dest}")
    for w in sorted(conv.warnings):
        print("Avertissement :", w)
