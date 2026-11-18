# Smart Campus Facility Booking and Maintenance Platform
Flask + SQLite + HTML/CSS/JS, deployed with Docker Compose.

## Run with Docker
```bash
docker compose up --build
```
Open http://localhost:5000. Set `SECRET_KEY=yourvalue docker compose up --build` for production. SQLite lives in the `campus_data` volume and survives restarts (`docker compose down -v` deletes it).

## Run locally
```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## Demo accounts
| Role | Email | Password |
|---|---|---|
| Admin | admin@campus.edu | admin123 |
| Student | student@campus.edu | student123 |
| Staff | staff@campus.edu | staff123 |
| Staff (technician) | tech@campus.edu | staff123 |

## Features
Role-based login/registration (CSRF + hashed passwords), facility CRUD, availability check and booking with overlap prevention, admin approve/reject, maintenance tickets with priority SLAs (high 24h, medium 72h, low 120h), admin assignment/status tracking, role-specific dashboards.
Admin accounts are seeded only; registration creates Student or Staff users.

## Structure
`app.py` backend and API · `templates/` pages · `static/` CSS/JS · `data/` SQLite file · `Dockerfile`, `docker-compose.yml`
