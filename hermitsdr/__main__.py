"""Entry point: python -m hermitsdr"""
import argparse
from .app import start_app

def main():
    parser = argparse.ArgumentParser(description='HermitSDR - Hermes Lite 2 Web Client')
    parser.add_argument('--host', default='0.0.0.0', help='Bind address')
    parser.add_argument('--port', type=int, default=5000, help='Port')
    parser.add_argument('--debug', action='store_true', help='Debug mode')
    args = parser.parse_args()
    start_app(host=args.host, port=args.port, debug=args.debug)

if __name__ == '__main__':
    main()
