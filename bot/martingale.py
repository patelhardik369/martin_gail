def next_bet(streak_type: str, streak_count: int, cfg: dict) -> dict:
    """Decide how many shares to buy on the next trade.

    `streak_type` is one of 'win', 'loss', 'none'. `streak_count` is the
    length of the current streak (0 if 'none').

    On a loss streak, bet = base * multiplier^min(streak_count, max_doubles).
    On a win streak or fresh start, bet = base.
    """
    base = int(cfg["base_shares"])
    multiplier = int(cfg["bet_multiplier"])
    max_step = int(cfg["max_doubles"])

    if streak_type == "loss":
        step = min(streak_count, max_step)
    else:
        step = 0

    shares = base * (multiplier ** step)
    return {"shares": int(shares), "step": step}
