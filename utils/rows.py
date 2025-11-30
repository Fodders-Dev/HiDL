def row_to_dict(row):
    """Safe convert sqlite Row or None to plain dict."""
    if row is None:
        return {}
    try:
        return dict(row)
    except Exception:
        return {}
