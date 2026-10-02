"""Ham gövdeyle dosya yükleme (multipart değil: ek bağımlılık yok). Gövde parça parça diske yazılır, boyut sınırlanır."""
import os

from fastapi import HTTPException, Request

from app.engine.references import MAX_UPLOAD_BYTES        # tek sınır: referanslar, stem'ler, videolar


def extension(filename: str, allowed: tuple[str, ...], what: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in allowed:
        raise HTTPException(422, f"{what} için desteklenen uzantılar: {', '.join(allowed)}")
    return ".jpg" if ext == ".jpeg" else ext


async def receive(request: Request, path: str) -> int:
    """İstek gövdesini `path`e yazar; boyutu döndürür. Boşsa 422, çok büyükse 413."""
    size = 0
    with open(path, "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Dosya çok büyük (en çok 200 MB)")
            f.write(chunk)
    if size == 0:
        raise HTTPException(422, "Boş dosya")
    return size
