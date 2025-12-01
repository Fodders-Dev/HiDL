def format_money(value: float) -> str:
    """Format money with thin separator and no decimals."""
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except Exception:
        return str(value)
