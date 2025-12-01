def row_to_dict(row):
    """Safe convert sqlite Row or None to plain dict."""
    if row is None:
        return {}
    try:
        return dict(row)
    except Exception:
        return {}


def rows_to_dicts(rows):
    """Convert iterable of rows to list of dicts, ignoring None."""
    out = []
    for row in rows or []:
        out.append(row_to_dict(row))
    return out
