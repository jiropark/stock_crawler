"""Flask 앱 팩토리"""

import os
from datetime import datetime
from flask import Flask


def create_app() -> Flask:
    """Flask 앱 생성"""
    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    static_dir = os.path.join(os.path.dirname(__file__), "static")

    app = Flask(
        __name__,
        template_folder=template_dir,
        static_folder=static_dir,
    )
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "stock-crawler-secret")

    # 라우트 등록
    from web.routes import bp
    app.register_blueprint(bp)

    # 모든 페이지에 공통 변수 전달
    @app.context_processor
    def inject_globals():
        from db import get_connection
        try:
            conn = get_connection()
            row = conn.execute(
                "SELECT collected_at FROM news ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.close()
            last_updated = row[0] if row else "아직 없음"
        except Exception:
            last_updated = "조회 실패"
        return {"last_updated": last_updated}

    return app
