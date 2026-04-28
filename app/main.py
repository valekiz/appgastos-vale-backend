"""
Punto de entrada FastAPI para AppGastos.
"""
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.models.database import init_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title="AppGastos API",
    description="Backend para seguimiento de gastos via cartolas Santander",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # restringir en producción si se desea
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


@app.on_event("startup")
def startup():
    try:
        init_db()
        logging.getLogger(__name__).info("Base de datos inicializada")
    except Exception as exc:
        logging.getLogger(__name__).error("Error inicializando DB: %s", exc)
        # No tumbar el proceso: Render health-check pasa y los requests
        # que toquen la DB fallarán individualmente con 500.


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=True)
