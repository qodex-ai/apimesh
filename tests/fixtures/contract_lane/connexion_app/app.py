import connexion


def list_notes():
    return []


app = connexion.FlaskApp(__name__, specification_dir="specs")
app.add_api("openapi.yaml", base_path="/v1")
