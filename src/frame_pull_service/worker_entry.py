import signal

from .config import get_settings
from .db import create_db_engine, init_db, make_session_factory
from .services.worker import Worker

def main() -> None:
    settings=get_settings(); settings.ensure_data_dirs(); engine=create_db_engine(settings); init_db(engine)
    worker=Worker(make_session_factory(engine), settings); worker.start()

    def request_stop(_signum=None, _frame=None) -> None:
        worker.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        while worker.active: worker.thread.join(1)
    except KeyboardInterrupt:
        worker.stop()
    finally:
        worker.stop()
        worker.wait_for_active_jobs()
        engine.dispose()


if __name__ == "__main__":
    main()
