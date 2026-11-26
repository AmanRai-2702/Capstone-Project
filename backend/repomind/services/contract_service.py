"""API contracts: what each endpoint returns and accepts versus what its clients send and read.

Everything here is read from the code (see PythonASTVisitor._endpoint_contract and the JavaScript analyzer's
`_contract_metadata`); nothing is executed. `None`/unknown is kept distinct from "nothing": a client whose
response handling cannot be read statically is reported as *possibly* affected, never as unaffected.

Two questions are answered:
  * contracts()      - which endpoint/client pairs disagree today (a client reads a key the handler never
                       returns; a handler requires a field the client does not send; ...)
  * change_impact()  - if these response keys / request fields change, exactly which clients and handlers
                       are affected, and which only *might* be.
"""
from __future__ import annotations

from repomind.graph import RepositoryGraph
from repomind.models import RelationType


class ContractService:
    def __init__(self, graph: RepositoryGraph):
        self.graph = graph

    # ------------------------------------------------------------------ helpers
    def _location(self, edge: dict) -> str | None:
        for ev in edge.get("evidence") or []:
            loc = ev.get("location")
            if loc:
                return f"{loc['file_path']}:{loc['start_line']}-{loc['end_line']}"
        return None

    def _endpoint(self, endpoint_id: str) -> dict:
        e = self.graph.get_entity(endpoint_id)
        meta = e.metadata if e else {}
        handlers = [x["source"] for x in self.graph.incoming(endpoint_id, [RelationType.HANDLES])]
        clients = []
        for edge in self.graph.incoming(endpoint_id, [RelationType.REQUESTS]):
            m = edge.get("metadata") or {}
            clients.append({"client": edge["source"], "sent_keys": m.get("sent_keys"), "read_keys": m.get("read_keys"),
                            "kind": edge["kind"], "confidence": edge["confidence"], "location": self._location(edge)})
        return {
            "endpoint": endpoint_id, "method": meta.get("method"), "route": meta.get("route"), "handlers": handlers,
            "response_keys": meta.get("response_keys"), "response_complete": bool(meta.get("response_complete")),
            "request_fields": meta.get("request_fields"), "clients": clients,
        }

    # ------------------------------------------------------------------ current agreement
    def contract(self, endpoint_id: str) -> dict:
        c = self._endpoint(endpoint_id)
        issues = []
        returned = set(c["response_keys"] or [])
        fields = c["request_fields"] or {}
        if c["response_complete"]:
            for cl in c["clients"]:
                if cl["read_keys"] is None:
                    continue
                for key in sorted(set(cl["read_keys"]) - returned):
                    issues.append({"severity": "high", "type": "response_key_not_returned", "client": cl["client"],
                                   "key": key, "location": cl["location"],
                                   "detail": f"client reads '{key}' but the handler only returns "
                                             f"{sorted(returned) or 'no keys'}"})
        for cl in c["clients"]:
            if cl["sent_keys"] is None or not fields:
                continue
            sent = set(cl["sent_keys"])
            for key in sorted(k for k, required in fields.items() if required and k not in sent):
                issues.append({"severity": "high", "type": "required_field_not_sent", "client": cl["client"],
                               "key": key, "location": cl["location"],
                               "detail": f"the handler requires '{key}' but this client does not send it"})
            for key in sorted(sent - set(fields)):
                issues.append({"severity": "medium", "type": "unknown_field_sent", "client": cl["client"],
                               "key": key, "location": cl["location"],
                               "detail": f"client sends '{key}', which the handler does not read"})
        readers = [cl for cl in c["clients"]]
        if returned and readers and all(cl["read_keys"] is not None for cl in readers):
            used = {k for cl in readers for k in cl["read_keys"]}
            for key in sorted(returned - used):
                issues.append({"severity": "info", "type": "response_key_unused", "client": None, "key": key,
                               "location": None, "detail": f"the handler returns '{key}' but no client reads it"})
        c["issues"] = issues
        return c

    def contracts(self) -> list[dict]:
        """Every endpoint that has a handler or a client, with its contract and issues."""
        out = []
        for eid, e in sorted(self.graph.entities.items()):
            if e.type.value == "api_endpoint":
                c = self.contract(eid)
                if c["handlers"] or c["clients"]:
                    out.append(c)
        return out

    # ------------------------------------------------------------------ what would a change break
    def change_impact(self, endpoint_id: str, response_keys: list[str] | None = None,
                      request_fields: list[str] | None = None) -> dict:
        """Who is affected if these response keys are renamed/removed or these request fields change.
        certain  - the client demonstrably reads / sends that key
        possible - the client's response handling (or body) cannot be read statically, so it might"""
        c = self._endpoint(endpoint_id)
        response_keys, request_fields = set(response_keys or []), set(request_fields or [])
        affected, handler_affected = [], []
        for cl in c["clients"]:
            reasons = []
            if response_keys:
                if cl["read_keys"] is None:
                    reasons.append(("possible", f"cannot tell whether it reads {sorted(response_keys)}"))
                else:
                    hit = sorted(response_keys & set(cl["read_keys"]))
                    if hit:
                        reasons.append(("certain", f"reads {hit}"))
            if request_fields:
                if cl["sent_keys"] is None:
                    reasons.append(("possible", f"cannot tell whether it sends {sorted(request_fields)}"))
                else:
                    hit = sorted(request_fields & set(cl["sent_keys"]))
                    if hit:
                        reasons.append(("certain", f"sends {hit}"))
            if reasons:
                certainty = "certain" if any(r[0] == "certain" for r in reasons) else "possible"
                affected.append({"client": cl["client"], "certainty": certainty, "reason": "; ".join(r[1] for r in reasons),
                                 "location": cl["location"]})
        if response_keys:
            returned = set(c["response_keys"] or [])
            if response_keys & returned or not c["response_complete"]:
                handler_affected = c["handlers"]               # the code that builds the response has to change
        if request_fields and c["handlers"]:
            handler_affected = c["handlers"]
        return {"endpoint": endpoint_id, "response_keys": sorted(response_keys), "request_fields": sorted(request_fields),
                "handlers": handler_affected, "clients": affected,
                "unaffected_clients": [cl["client"] for cl in c["clients"] if cl["client"] not in {a["client"] for a in affected}]}
