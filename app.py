"""Flask entry point. Web server and scheduler intentionally run as separate processes."""
from sahayak import create_app
app = create_app()

if __name__ == '__main__':
    from waitress import serve
    print('SAHAYAK is available at http://127.0.0.1:5000')
    print('Keep this terminal open. Use a second terminal for the scheduler (see README).')
    serve(app, host='127.0.0.1', port=5000, threads=4)
