Swagger UI static assets (swagger-ui-dist 5.32.6, Apache-2.0, https://github.com/swagger-api/swagger-ui), as shipped in
@fastify/swagger-ui 6.1.2 `static/` (`index.css` is that package's). Served by `app/main.py` at `/api/docs/static/*`
(override the directory with `SWAGGER_UI_DIR`). Vendored so the Python image needs no Node packages at runtime.
