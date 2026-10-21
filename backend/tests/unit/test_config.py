"""Configuration: defaults, validation messages, production refusals, removed settings."""

import pytest

from app.config import DEV_MFA_KEY, ConfigError, load_config

GOOD_KEY = 'x' * 48
PROD = {
    'APP_ENV': 'production',
    'MFA_ENCRYPTION_KEY': GOOD_KEY,
    'DATABASE_URL': 'postgres://seereon_app:s3cret-Pa55@db:5432/seereonos',
    'APP_URL': 'https://app.example.com',
    'CORS_ORIGINS': 'https://app.example.com',
    'SMTP_URL': 'smtp://mailer:pw@smtp.example.com:587',
}


def test_defaults():
    c = load_config({})
    assert (c.env, c.port, c.rate_limit_global, c.rate_limit_auth, c.session_idle_hours, c.static_dir) == ('development', 8000, 600, 10, 12, '../frontend/dist')
    assert c.mfa_encryption_key == DEV_MFA_KEY and c.mfa_key_is_default and c.freeze_rows
    assert c.database_url == 'postgres://seereon_app:seereon_app@localhost:5432/seereonos'
    assert c.cors_origins == ('http://localhost:5173',) and c.trust_proxy == 0 and c.email_delivery == 'disabled' and not c.payments_enabled
    assert load_config({'NODE_ENV': 'test'}).env == 'test' and load_config({'NODE_ENV': 'test'}).cors_origins == ()
    assert load_config({'STATIC_DIR': 'dist'}).static_dir == 'dist' and load_config({'STATIC_DIR': ''}).static_dir is None
    assert load_config(PROD).static_dir is None and load_config({'APP_ENV': 'test'}).static_dir is None
    assert load_config({'PORT': ' 9000 '}).port == 9000 and load_config(PROD).freeze_rows is False
    p = load_config(PROD)
    assert p.email_delivery == 'smtp' and p.app_url == 'https://app.example.com' and p.cors_origins == ('https://app.example.com',)


@pytest.mark.parametrize(
    'env, msg',
    [
        ({'PORT': '0'}, 'PORT must be an integer between 1 and 65535 (got "0").'),
        ({'PORT': 'abc'}, 'PORT must be an integer between 1 and 65535 (got "abc").'),
        ({'TRUST_PROXY': 'maybe'}, 'TRUST_PROXY must be false, true (one proxy) or the number of trusted proxies 1-9 (got "maybe").'),
        ({'DATABASE_URL': 'mysql://u:p@h/db'}, 'DATABASE_URL must be a postgres: or postgresql: URL (got "mysql://***@h/db").'),
        ({'CORS_ORIGINS': 'https://a.com/path'}, 'CORS_ORIGINS entries must be origins like https://app.example.com (got "https://a.com/path").'),
        ({'CORS_ORIGINS': '*'}, 'CORS_ORIGINS must list exact origins; "*" is never allowed.'),
        ({'MFA_ENCRYPTION_KEY': 'short'}, 'MFA_ENCRYPTION_KEY must be at least 32 characters'),
        ({'APP_ENV': 'staging'}, 'APP_ENV must be development, production or test (got "staging").'),
        ({'EMAIL_DELIVERY': 'smtp'}, 'EMAIL_DELIVERY=smtp needs SMTP_URL'),
        ({'RAZORPAY_KEY_ID': 'rzp_test_x'}, 'Razorpay needs both RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET'),
    ],
)
def test_validation_messages(env, msg):  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigError) as e:
        load_config(env)
    assert msg in str(e.value)


@pytest.mark.parametrize('key', ['DATABASE_APP_URL', 'DEMO_MODE', 'SEED_DEMO', 'SEED_JSON'])
def test_removed_settings_are_refused_everywhere(key):  # type: ignore[no-untyped-def]
    for env in ({key: 'x'}, {**PROD, key: 'x'}):
        with pytest.raises(ConfigError) as e:
            load_config(env)
        assert f'{key} was removed' in str(e.value)


def test_production_refusals():
    load_config(PROD)  # ok
    for over, msg in (
        ({'MFA_ENCRYPTION_KEY': ''}, 'MFA_ENCRYPTION_KEY is required when APP_ENV=production'),
        ({'MFA_ENCRYPTION_KEY': 'CHANGE_ME_openssl_rand_base64_48_xxxxx'}, 'must not be the placeholder'),
        ({'DATABASE_URL': ''}, 'DATABASE_URL is required when APP_ENV=production'),
        ({'DATABASE_URL': 'postgres://seereon_app:seereon_app@db/x'}, 'DATABASE_URL uses a default, empty or placeholder password'),
        ({'DATABASE_URL': 'postgres://seereon_app:CHANGE_ME@db/x'}, 'DATABASE_URL uses a default, empty or placeholder password'),
        ({'DATABASE_URL': 'postgres://seereon_app@db/x'}, 'DATABASE_URL uses a default, empty or placeholder password'),
        ({'APP_URL': ''}, 'APP_URL is required when APP_ENV=production'),
        ({'APP_URL': 'http://app.example.com'}, 'APP_URL must be the public https URL'),
        ({'APP_URL': 'https://localhost:8000'}, 'APP_URL must be the public https URL'),
        ({'CORS_ORIGINS': ''}, 'CORS_ORIGINS is required when APP_ENV=production'),
        ({'CORS_ORIGINS': 'http://app.example.com'}, 'CORS_ORIGINS must use https origins in production'),
        ({'SMTP_URL': ''}, 'SMTP is required when APP_ENV=production'),
        ({'RAZORPAY_KEY_ID': 'rzp_live_x', 'RAZORPAY_KEY_SECRET': 's'}, 'RAZORPAY_WEBHOOK_SECRET is required'),
        ({'METRICS_TOKEN': 'short'}, 'METRICS_TOKEN must be at least 24 characters'),
        ({'SMTP_URL': 'smtps://CHANGE_ME:CHANGE_ME@smtp.example.com:465'}, 'SMTP_URL still contains a CHANGE_ME placeholder'),
    ):
        with pytest.raises(ConfigError) as e:
            load_config({**PROD, **over})
        assert msg in str(e.value), (over, str(e.value))


def test_production_email_can_be_disabled_explicitly():
    c = load_config({**PROD, 'SMTP_URL': '', 'EMAIL_DELIVERY': 'disabled'})
    assert c.email_delivery == 'disabled'


def test_api_docs_off_by_default_in_production():
    assert load_config({}).api_docs is True and load_config({'APP_ENV': 'test'}).api_docs is True
    assert load_config(PROD).api_docs is False
    assert load_config({**PROD, 'API_DOCS_ENABLED': 'true'}).api_docs is True
    assert load_config({'API_DOCS_ENABLED': 'false'}).api_docs is False
    with pytest.raises(ConfigError, match='API_DOCS_ENABLED must be true or false'):
        load_config({'API_DOCS_ENABLED': 'maybe'})


def test_trust_proxy_hops():
    assert [load_config({'TRUST_PROXY': v}).trust_proxy for v in ('', 'false', 'true', '2')] == [0, 0, 1, 2]


def test_client_ip_uses_trusted_hops_from_the_right():
    from types import SimpleNamespace

    from app.main import client_ip

    def req(xff: str | None, peer: str = '10.0.0.5'):  # type: ignore[no-untyped-def]
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers={'x-forwarded-for': xff} if xff is not None else {})

    assert client_ip(req('6.6.6.6, 1.2.3.4'), 0) == '10.0.0.5'  # not behind a trusted proxy: never trust the header
    assert client_ip(req('6.6.6.6, 1.2.3.4'), 1) == '1.2.3.4'  # the spoofed left entry is ignored
    assert client_ip(req('6.6.6.6, 1.2.3.4, 10.1.1.1'), 2) == '1.2.3.4'
    assert client_ip(req(None), 1) == '10.0.0.5'
