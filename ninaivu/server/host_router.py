"""Share the family listener with an explicitly named admin virtual host."""
from ..utils.discovery import normalise_name


def admin_hostname(cfg, args):
    # Never expose a console restricted to another bind address on the family listener.
    if args.no_admin or args.no_mdns or not args.name or cfg.admin_host != cfg.host:
        return None
    family = normalise_name(args.name)
    admin = normalise_name(args.admin_name if args.admin_name is not None else f'{family}-admin')
    return f'{admin}.local' if admin != family else None


class HostRouter:
    def __init__(self, home, admin, hostname):
        self.home, self.admin, self.hostname = home, admin, hostname

    def __call__(self, environ, start_response):
        host = environ.get('HTTP_HOST', '').split(':', 1)[0].lower().rstrip('.')
        app = self.admin if host == self.hostname else self.home
        return app(environ, start_response)
