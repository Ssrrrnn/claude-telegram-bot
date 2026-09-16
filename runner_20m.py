import runner_strict_bubbles as app

# Override proactive contact cadence without changing the rest of the roleplay,
# memory, time-awareness, and strict-bubble behavior.
app.PROACTIVE_MIN_SECONDS = 20 * 60
app.PROACTIVE_MAX_SECONDS = 60 * 60

if __name__ == "__main__":
    app.runner.main()
