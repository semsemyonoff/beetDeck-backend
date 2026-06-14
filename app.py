"""beetDeek entry point — thin wrapper around the beetdeck app factory."""

from beetdeck import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
