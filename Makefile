.PHONY: up down reset samples smoke test logs ps

up:        ## Tüm servisleri derle ve başlat
	docker compose up -d --build

samples:   ## Sahte üretim için örnek video/ses dosyalarını oluştur
	docker compose run --rm api python -m scripts.make_samples

smoke:     ## Uçtan uca duman testi
	docker compose exec api python -m scripts.smoke

test:      ## Birim testleri
	docker compose run --rm api python -m pytest -q

logs:
	docker compose logs -f api worker

ps:
	docker compose ps

down:
	docker compose down

reset:     ## Veritabanı ve MinIO dahil her şeyi sil
	docker compose down -v
