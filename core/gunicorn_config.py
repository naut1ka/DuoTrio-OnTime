def post_worker_init(worker):
    from reminders import start_scheduler
    start_scheduler()

