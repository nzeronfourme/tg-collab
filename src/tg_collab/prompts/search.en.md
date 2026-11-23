# Telegram peer search (fast)

The owner runs the Telegram channel @$USERNAME. Find public peer channels for one personal,
white-hat collaboration message.

- Topics: $TERMS
- Hashtags: $HASHTAGS. Link domains: $DOMAINS.
- Language: $LANGUAGE. Region: $REGION.
- Size: $SIZE_RANGE. Prefer this range, but include relevant channels outside it; the tool scores
  size separately.

Rules:
- Use at most 3 web queries. Do not open `t.me` pages; the tool checks channels itself.
- Stop after 90 seconds.
- Return 5-10 channels (at most 10 channels), or at least 3 if you really cannot find more.
- Return ONLY a JSON array, no prose: [{"username": "name", "why": "one sentence"}]. `username`
  is a real public Telegram username without `@`; never invent one.

The tool and the agent never send anything to Telegram or contact anyone. Do not join channels.
This brief only prepares a list for the human owner.

