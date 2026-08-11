import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
import google.cloud.firestore as fs


@asynccontextmanager
async def lifespan(application):
    application.state.db = fs.Client(project=os.environ["GE_FIRESTORE_PROJECT_ID"])
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}
