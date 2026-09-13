from pathlib import Path
from urllib.parse import urljoin

from flask import Flask, url_for
from src.backend.server.versioned_static import install_versioned_static


def test_release_paths_include_relative_modules_and_styles(tmp_path):
    (tmp_path / 'js').mkdir()
    (tmp_path / 'js' / 'main.js').write_text("import './apiService.js';")
    (tmp_path / 'js' / 'apiService.js').write_text('export const release = 2;')
    (tmp_path / 'styles.css').write_text('body {}')
    app = Flask(__name__, static_folder=str(tmp_path), static_url_path='/static')
    install_versioned_static(app, 'release-2')
    with app.test_request_context():
        entry = url_for('static', filename='js/main.js')
        css = url_for('static', filename='styles.css')
    assert entry.startswith('/static/build/')
    client = app.test_client()
    assert client.get(entry).data == b"import './apiService.js';"
    assert client.get(urljoin(entry, './apiService.js')).data == b'export const release = 2;'
    assert client.get(css).status_code == 200
    assert client.get('/static/build/old-release/js/main.js').status_code == 404
    assert client.get(entry.replace('/js/main.js', '/../../private.env')).status_code == 404


def test_build_change_changes_entry_url_without_rewriting_templates(tmp_path):
    urls=[]
    for version in ('release-1','release-2'):
        app=Flask(__name__,static_folder=str(tmp_path),static_url_path='/static')
        install_versioned_static(app,version)
        with app.test_request_context():
            urls.append(url_for('static',filename='js/main.js'))
    assert urls[0] != urls[1]


def test_outage_worker_has_narrow_scope_and_release_specific_public_cache():
    root = Path(__file__).resolve().parents[2] / 'src/frontend/web/von_interface/static'
    app = Flask(__name__, static_folder=str(root), static_url_path='/static')
    install_versioned_static(app, 'outage-fixture')
    response = app.test_client().get('/von/outage-worker.js')
    assert response.status_code == 200
    assert response.headers['Service-Worker-Allowed'] == '/von/'
    assert response.headers['Cache-Control'] == 'no-cache'
    assert "const CACHE = 'von-outage-v1-" in response.text
    assert 'session' not in response.text.lower()
