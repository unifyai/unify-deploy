import os
from fastapi import FastAPI
from communication.phone.views import router as phone_router
from communication.whatsapp.views import router as whatsapp_router
from communication.email.views import router as email_router
import uvicorn

from dotenv import load_dotenv
load_dotenv()

app = FastAPI()
app.include_router(phone_router, prefix="/phone")
app.include_router(whatsapp_router, prefix="/whatsapp")
app.include_router(email_router, prefix="/email")

@app.get("/")
async def read_root():
    return {"message": "success!"}

if __name__ == "__main__":
    uvicorn.run(app, host='0.0.0.0', port=8080)