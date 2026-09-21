from .config import get_settings
from .db import create_db_engine, init_db, make_session_factory
from .services.worker import Worker

if __name__ == "__main__":
    settings=get_settings(); settings.ensure_data_dirs(); engine=create_db_engine(settings); init_db(engine)
    worker=Worker(make_session_factory(engine), settings); worker.start()
    try:
        while worker.active: worker.thread.join(1)
    except KeyboardInterrupt:
        worker.stop()
