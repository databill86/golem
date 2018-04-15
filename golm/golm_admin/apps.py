from django.apps import AppConfig


class GolemConfig(AppConfig):
    name = 'golm_main'

    def ready(self):
        print('Init webhooks @ GolemConfig')
        from core.interfaces.all import init_webhooks
        init_webhooks()