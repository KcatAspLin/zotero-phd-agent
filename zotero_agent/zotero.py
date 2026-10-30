"""Zotero v3: collection routing, versioned edits and the three-step file upload."""
import json
import re
import secrets
import uuid
from pathlib import Path

from .content import CATEGORIES, file_hash, normalize_doi
from .net import AgentError, HttpError

MARKER = "ZoteroAgent: "


def new_key():
    return "".join(secrets.choice("23456789ABCDEFGHIJKLMNPQRSTUVWXYZ") for _ in range(8))


def merged_tags(data, tags):
    previous = []
    for line in data.get("extra", "").splitlines():
        if line.startswith(MARKER):
            try:
                previous = json.loads(line[len(MARKER):]).get("tags", [])
            except (ValueError, AttributeError):
                pass
    old = {t.casefold() for t in previous if isinstance(t, str)}
    result = [t for t in data.get("tags", []) if not (t.get("type") == 1 and t["tag"].casefold() in old)]
    present = {t["tag"].casefold() for t in result}
    for tag in tags:
        if tag.casefold() not in present:
            result.append({"tag": tag, "type": 1})
            present.add(tag.casefold())
    return result


class Zotero:
    def __init__(self, http, library_id, library_type, key):
        if library_type not in ("user", "group") or not re.fullmatch(r"\d+", library_id):
            raise AgentError("Set a numeric ZOTERO_LIBRARY_ID and type user or group")
        if not key:
            raise AgentError("Set ZOTERO_API_KEY in .env")
        self.http = http
        self.prefix = f"https://api.zotero.org/{library_type}s/{library_id}"
        self.headers = {"Zotero-API-Key": key, "Zotero-API-Version": "3"}

    def call(self, method, path, **kwargs):
        headers = {**self.headers, **kwargs.pop("headers", {})}
        return self.http.request(method, self.prefix + "/" + path, headers=headers, **kwargs)

    def get(self, path):
        try:
            return self.call("GET", path).json()
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise

    def all(self, path):
        result, start = [], 0
        while True:
            sep = "&" if "?" in path else "?"
            page = self.call("GET", f"{path}{sep}limit=100&start={start}").json()
            if not isinstance(page, list):
                raise AgentError("Unexpected Zotero list response")
            result.extend(page)
            if len(page) < 100:
                return result
            start += len(page)

    def create(self, endpoint, payload):
        # The caller saves the object key before this request. Never blindly retry POST.
        result = self.call("POST", endpoint, json_data=[payload],
                           headers={"Zotero-Write-Token": uuid.uuid4().hex}).json()
        if result.get("failed"):
            failure = next(iter(result["failed"].values()))
            raise AgentError(f"Zotero rejected {endpoint} write (code {failure.get('code', 'unknown')})")
        if "0" in result.get("successful", {}):
            return result["successful"]["0"]
        if "0" in result.get("unchanged", {}):
            return self.get(f"{endpoint}/{result['unchanged']['0']}")
        raise AgentError("Zotero did not confirm the write; rerun to reconcile")

    def ensure_collection(self, name, parent, journal, save):
        matches = [c for c in self.all("collections") if c["data"]["name"] == name
                   and (c["data"].get("parentCollection") or False) == (parent or False)]
        if len(matches) > 1:
            raise AgentError(f"Multiple collections named {name!r} under the same parent; resolve in Zotero")
        if matches:
            return matches[0]["key"]
        slot = f"collection:{parent or 'ROOT'}:{name}"
        key = journal.setdefault(slot, new_key())
        save()
        existing = self.get(f"collections/{key}")
        if existing:
            if existing["data"]["name"] != name or (existing["data"].get("parentCollection") or False) != (parent or False):
                raise AgentError("Reserved collection key belongs to a different collection")
            return key
        self.create("collections", {"key": key, "version": 0, "name": name, "parentCollection": parent or False})
        return key

    def collections(self, journal, save, pinned=""):
        if pinned:
            existing = self.get(f"collections/{pinned}")
            if not existing or existing["data"]["name"] != "PhD":
                raise AgentError("ZOTERO_PHD_COLLECTION_KEY must identify a collection named PhD")
            root = pinned
        else:
            root = self.ensure_collection("PhD", False, journal, save)
        children = {name: self.ensure_collection(name, root, journal, save) for name in CATEGORIES}
        return root, children

    def find_doi(self, doi):
        # Paginate all top-level records: exact DOI equality, independent of full-text indexing.
        matches = []
        for item in self.all("items/top"):
            value = item["data"].get("DOI", "")
            if not value:
                continue
            try:
                if normalize_doi(value) == doi:
                    matches.append(item)
            except AgentError:
                continue
        if len(matches) > 1:
            raise AgentError("Multiple Zotero items have this DOI; merge them in Zotero before importing")
        return matches[0] if matches else None

    def import_plan(self, plan, journal, save, pinned=""):
        root, children = self.collections(journal, save, pinned)
        item = self.find_doi(plan["doi"])
        if item:
            journal["parent_key"] = item["key"]
            save()
        else:
            key = journal.setdefault("parent_key", new_key())
            save()
            item = self.get(f"items/{key}")
            if item:
                if normalize_doi(item["data"].get("DOI", "")) != plan["doi"]:
                    raise AgentError("Reserved item key belongs to a different DOI")
            else:
                fields = {k: v for k, v in plan["metadata"].items() if k not in ("metadata_source", "abstract_source")}
                item = self.create("items", {"key": key, "version": 0, "itemType": "journalArticle",
                                               "tags": [], "collections": [], "relations": {}, **fields})
        if item["data"].get("itemType") != "journalArticle":
            raise AgentError("Existing DOI item is not a journal article; reconcile its item type manually")
        key, data = item["key"], item["data"]
        patch = {}
        preserved = []
        for field, value in plan["metadata"].items():
            if field in ("metadata_source", "abstract_source"):
                continue
            if value and not data.get(field):
                patch[field] = value
            elif value and data.get(field) != value:
                preserved.append(field)
        categories = set(children.values())
        chosen = children[plan["category"]]
        patch["collections"] = list(dict.fromkeys([c for c in data.get("collections", []) if c not in categories] + [root, chosen]))
        patch["tags"] = merged_tags(data, plan["tags"])
        extra = "\n".join(line for line in data.get("extra", "").splitlines() if not line.startswith(MARKER))
        record = {"tags": plan["tags"], "category": plan["category"], "metadata": plan["metadata"]["metadata_source"],
                  "abstract": plan["metadata"]["abstract_source"], "model": plan["model"]}
        patch["extra"] = (extra + "\n" + MARKER + json.dumps(record, ensure_ascii=False)).lstrip("\n")
        patch = {k: v for k, v in patch.items() if data.get(k) != v}
        if patch:
            self.call("PATCH", f"items/{key}", json_data=patch,
                      headers={"If-Unmodified-Since-Version": str(item["version"])})
        journal["phase"] = "metadata_saved"
        save()
        attachment = self.attach(key, Path(plan["pdf"]), journal, save)
        journal["phase"] = "complete"
        save()
        return {"item_key": key, "attachment_key": attachment, "collection": "PhD/" + plan["category"],
                "generated_tags": plan["tags"], "preserved_existing_fields": preserved, "status": "complete"}

    def attach(self, parent, pdf, journal, save):
        md5 = file_hash(pdf, "md5")
        children = self.all(f"items/{parent}/children")
        for child in children:
            data = child["data"]
            if data.get("itemType") == "attachment" and data.get("md5") == md5:
                return child["key"]
        key = journal.setdefault("attachment_key", new_key())
        save()
        child = self.get(f"items/{key}")
        if child:
            data = child["data"]
            if data.get("parentItem") != parent or data.get("itemType") != "attachment":
                raise AgentError("Reserved attachment key belongs to another item")
            if data.get("md5") == md5:
                return key
            if data.get("md5"):
                raise AgentError("Attachment changed remotely; refusing to replace it")
        else:
            self.create("items", {"key": key, "version": 0, "itemType": "attachment", "parentItem": parent,
                                  "linkMode": "imported_file", "title": "Full Text PDF", "filename": pdf.name,
                                  "contentType": "application/pdf", "tags": [], "relations": {}})
        journal["phase"] = "attachment_created"
        save()
        headers = {"If-None-Match": "*"}
        auth = self.call("POST", f"items/{key}/file", headers=headers, form={
            "md5": md5, "filename": pdf.name, "filesize": pdf.stat().st_size,
            "mtime": int(pdf.stat().st_mtime * 1000)}).json()
        if not auth.get("exists"):
            body = auth["prefix"].encode() + pdf.read_bytes() + auth["suffix"].encode()
            # Deliberately use a separate request without Zotero authentication headers.
            self.http.request("POST", auth["url"], data=body, headers={"Content-Type": auth["contentType"]})
            self.call("POST", f"items/{key}/file", headers=headers, form={"upload": auth["uploadKey"]})
        remote = self.get(f"items/{key}")
        if not remote or remote["data"].get("md5") != md5:
            raise AgentError("Upload could not be verified; rerun to resume")
        return key
