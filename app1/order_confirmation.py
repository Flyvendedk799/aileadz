"""The written confirmation the assistant gives once an order is confirmed.

Clicking Bekræft on the order card runs create_course_order straight from the
confirm route, without a model turn. The card alone is not a reply, so the route
builds the sentence here from the tool result and the chat shows it as an
assistant message (and stores it in the transcript, so the next turn knows the
order exists). Danish, plain, and only claims what the tool result says.
"""

# Statuses of create_course_order that mean "something was booked or handed on".
CONFIRMED_ORDER_STATUSES = ("order_created", "team_orders_created", "handed_off_to_hr")


def _course_title(args):
    """Title of the booked course from the stored tool args, or ''."""
    handle = (args or {}).get("product_handle") or ""
    if not handle:
        return ""
    try:
        import catalog_service as catalog
        product = catalog.get_product(handle)
        return (product or {}).get("title") or ""
    except Exception:
        return ""


def order_confirmation_text(result, args=None):
    """Danish confirmation for a confirmed order result, or '' if none applies."""
    if not isinstance(result, dict):
        return ""
    status = result.get("status")
    if status not in CONFIRMED_ORDER_STATUSES:
        return ""
    if status != "order_created":
        # Team and HR hand-offs already carry a short Danish sentence.
        return (result.get("message") or "").strip()

    title = _course_title(args)
    text = f"Din bestilling af **{title}** er registreret." if title else "Din bestilling er registreret."
    if result.get("needs_approval"):
        text += " Den afventer godkendelse, og du får besked, når den er behandlet."
    elif result.get("order_status_label"):
        text += f" Status: {result['order_status_label']}."
    url = result.get("order_url")
    if url:
        text += f" Du kan følge den her: [Se bestillingen]({url})."
    return text
