"""SCIM 2.0 /Groups mapped to departments (N-7.2).

An IdP group ("Salg", "HR") IS a department: ``company_departments`` is the
group list and a user is a member when ``company_users.department`` equals the
department name. Everything is scoped to the authenticated tenant
(``g.company_id``); the routes register on the existing ``scim_bp`` so they share
the enterprise API-key auth, rate limiting and the SCIM content type.

Member ids are the SCIM User ids (``company_users.id``) the Users endpoints hand out.
"""

from __future__ import annotations

import logging

from flask import g, request

from scim_api import (SCHEMA_LIST_RESPONSE, SCHEMA_PATCH_OP, _cursor, _scim_auth, _scim_error,
                      _scim_response, scim_bp)

logger = logging.getLogger(__name__)

SCHEMA_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"


def _members(cur, company_id, dept_name):
    cur.execute(
        "SELECT id, username, email FROM company_users WHERE company_id = %s AND department = %s "
        "AND status = 'active' ORDER BY id",
        (company_id, dept_name),
    )
    return [{"value": str(r["id"]), "display": r.get("username") or r.get("email") or "",
             "$ref": "/scim/v2/Users/%s" % r["id"]} for r in (cur.fetchall() or [])]


def _resource(cur, company_id, row):
    return {
        "schemas": [SCHEMA_GROUP],
        "id": str(row["id"]),
        "displayName": row["department_name"],
        "members": _members(cur, company_id, row["department_name"]),
        "meta": {"resourceType": "Group", "location": "/scim/v2/Groups/%s" % row["id"]},
    }


def _get_dept(cur, company_id, group_id):
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return None
    cur.execute("SELECT id, department_name FROM company_departments WHERE id = %s AND company_id = %s",
                (gid, company_id))
    return cur.fetchone()


def _set_members(cur, company_id, dept_name, member_ids, replace=False):
    """Put the given SCIM user ids in the department (same tenant only)."""
    ids = []
    for m in member_ids or []:
        try:
            ids.append(int(m.get("value") if isinstance(m, dict) else m))
        except (TypeError, ValueError):
            continue
    if replace:
        cur.execute("UPDATE company_users SET department = NULL WHERE company_id = %s AND department = %s",
                    (company_id, dept_name))
    for uid in ids:
        cur.execute("UPDATE company_users SET department = %s WHERE id = %s AND company_id = %s",
                    (dept_name, uid, company_id))


@scim_bp.route("/scim/v2/Groups", methods=["GET"])
@_scim_auth("read:employees")
def list_groups():
    try:
        cur = _cursor()
        cur.execute("SELECT id, department_name FROM company_departments WHERE company_id = %s "
                    "AND department_name IS NOT NULL AND department_name <> '' ORDER BY department_name",
                    (g.company_id,))
        rows = cur.fetchall() or []
        name = (request.args.get("filter") or "")
        if "displayName eq" in name:
            wanted = name.split("eq", 1)[1].strip().strip('"').lower()
            rows = [r for r in rows if (r["department_name"] or "").lower() == wanted]
        res = [_resource(cur, g.company_id, r) for r in rows]
        cur.close()
        return _scim_response({"schemas": [SCHEMA_LIST_RESPONSE], "totalResults": len(res),
                               "startIndex": 1, "itemsPerPage": len(res), "Resources": res})
    except Exception as e:
        logger.warning("scim groups list failed: %s", e)
        return _scim_error(500, "Grupperne kunne ikke hentes.")


@scim_bp.route("/scim/v2/Groups/<group_id>", methods=["GET"])
@_scim_auth("read:employees")
def get_group(group_id):
    try:
        cur = _cursor()
        row = _get_dept(cur, g.company_id, group_id)
        if not row:
            cur.close()
            return _scim_error(404, "Gruppen blev ikke fundet.")
        res = _resource(cur, g.company_id, row)
        cur.close()
        return _scim_response(res)
    except Exception as e:
        logger.warning("scim group get failed: %s", e)
        return _scim_error(500, "Gruppen kunne ikke hentes.")


@scim_bp.route("/scim/v2/Groups", methods=["POST"])
@_scim_auth("write:employees")
def create_group():
    data = request.get_json(silent=True) or {}
    name = (data.get("displayName") or "").strip()[:100]
    if not name:
        return _scim_error(400, "displayName er påkrævet.", "invalidValue")
    try:
        cur = _cursor()
        cur.execute("SELECT id, department_name FROM company_departments WHERE company_id = %s "
                    "AND LOWER(department_name) = LOWER(%s)", (g.company_id, name))
        if cur.fetchone():
            cur.close()
            return _scim_error(409, "En gruppe med det navn findes allerede.", "uniqueness")
        cur.execute("INSERT INTO company_departments (company_id, department_name) VALUES (%s, %s)",
                    (g.company_id, name))
        gid = cur.lastrowid
        _set_members(cur, g.company_id, name, data.get("members"))
        from flask import current_app
        current_app.mysql.connection.commit()
        res = _resource(cur, g.company_id, {"id": gid, "department_name": name})
        cur.close()
        return _scim_response(res, status=201)
    except Exception as e:
        logger.warning("scim group create failed: %s", e)
        return _scim_error(500, "Gruppen kunne ikke oprettes.")


@scim_bp.route("/scim/v2/Groups/<group_id>", methods=["PUT", "PATCH"])
@_scim_auth("write:employees")
def update_group(group_id):
    data = request.get_json(silent=True) or {}
    try:
        from flask import current_app
        cur = _cursor()
        row = _get_dept(cur, g.company_id, group_id)
        if not row:
            cur.close()
            return _scim_error(404, "Gruppen blev ikke fundet.")
        name = row["department_name"]
        if request.method == "PUT":
            new_name = (data.get("displayName") or name).strip()[:100]
            if new_name != name:
                cur.execute("UPDATE company_departments SET department_name = %s WHERE id = %s AND company_id = %s",
                            (new_name, row["id"], g.company_id))
                cur.execute("UPDATE company_users SET department = %s WHERE company_id = %s AND department = %s",
                            (new_name, g.company_id, name))
                name = new_name
            _set_members(cur, g.company_id, name, data.get("members") or [], replace=True)
        else:
            for op in data.get("Operations") or []:
                verb = (op.get("op") or "").lower()
                path = (op.get("path") or "").lower()
                value = op.get("value")
                if path == "displayname" or (not path and isinstance(value, dict) and "displayName" in value):
                    new_name = (value if isinstance(value, str) else value.get("displayName") or name).strip()[:100]
                    if new_name and new_name != name:
                        cur.execute("UPDATE company_departments SET department_name = %s WHERE id = %s AND company_id = %s",
                                    (new_name, row["id"], g.company_id))
                        cur.execute("UPDATE company_users SET department = %s WHERE company_id = %s AND department = %s",
                                    (new_name, g.company_id, name))
                        name = new_name
                elif path.startswith("members"):
                    if verb == "add":
                        _set_members(cur, g.company_id, name, value or [])
                    elif verb == "replace":
                        _set_members(cur, g.company_id, name, value or [], replace=True)
                    elif verb == "remove":
                        # path may be members[value eq "12"] or carry a value list
                        ids = [m.get("value") for m in (value or []) if isinstance(m, dict)]
                        if "value eq" in path:
                            ids.append(path.split("eq", 1)[1].strip(' "]'))
                        for uid in ids:
                            try:
                                cur.execute("UPDATE company_users SET department = NULL WHERE id = %s "
                                            "AND company_id = %s AND department = %s",
                                            (int(uid), g.company_id, name))
                            except (TypeError, ValueError):
                                continue
        current_app.mysql.connection.commit()
        res = _resource(cur, g.company_id, {"id": row["id"], "department_name": name})
        cur.close()
        return _scim_response(res)
    except Exception as e:
        logger.warning("scim group update failed: %s", e)
        return _scim_error(500, "Gruppen kunne ikke opdateres.")


@scim_bp.route("/scim/v2/Groups/<group_id>", methods=["DELETE"])
@_scim_auth("write:employees")
def delete_group(group_id):
    try:
        from flask import Response, current_app
        cur = _cursor()
        row = _get_dept(cur, g.company_id, group_id)
        if not row:
            cur.close()
            return _scim_error(404, "Gruppen blev ikke fundet.")
        cur.execute("UPDATE company_users SET department = NULL WHERE company_id = %s AND department = %s",
                    (g.company_id, row["department_name"]))
        cur.execute("DELETE FROM company_departments WHERE id = %s AND company_id = %s", (row["id"], g.company_id))
        current_app.mysql.connection.commit()
        cur.close()
        return Response(status=204)
    except Exception as e:
        logger.warning("scim group delete failed: %s", e)
        return _scim_error(500, "Gruppen kunne ikke slettes.")
