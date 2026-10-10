# M2 → M3 Launch Checklist

## Infrastructure

| Item | Owner | Status | Notes |
|------|-------|--------|-------|
| Domain name registered | User | ☐ | Point DNS to Fly.io or Railway |
| SSL/HTTPS configured | Code | ✅ | Fly: `force_https=true` in `fly.toml`; Railway: automatic |
| Health check reachable | Code | ✅ | `/healthz` returns 200 |

## Billing

| Item | Owner | Status | Notes |
|------|-------|--------|-------|
| Stripe or Paddle account created | User | ☐ | Choose provider before M3 |
| `BillingProvider` swapped to real impl | Code | ☐ | Implement `StripeBillingProvider` in `billing.py` |
| Webhook endpoint verified with real secret | Code | ☐ | `STRIPE_WEBHOOK_SECRET` must be set |
| Pricing updated from mock to real | User | ☐ | Update `PRICING_PLANS_JSON` |

## OCR

| Item | Owner | Status | Notes |
|------|-------|--------|-------|
| Aliyun OCR key configured | User | ☐ | Set `ALIYUN_OCR_KEY` in production env |
| OCR budget/cost cap reviewed | User | ☐ | Alipay/credit card spending limit on Aliyun |

## Security

| Item | Owner | Status | Notes |
|------|-------|--------|-------|
| Demo rate-limit thresholds reviewed | Code | ☐ | Current: IP 10/h, key 60/min — adjust for expected traffic |
| Privacy policy page | User | ☐ | Required for public launch |
| Terms of service page | User | ☐ | Required for public launch |
| `.env` / secrets not in repo | Code | ✅ | Verified via CI and `.gitignore` |

## Monitoring

| Item | Owner | Status | Notes |
|------|-------|--------|-------|
| Error tracking (Sentry or similar) | User | ☐ | Recommended before M3 |
| Uptime monitoring | User | ☐ | Fly/Railway built-in or external (UptimeRobot) |

## Marketing

| Item | Owner | Status | Notes |
|------|-------|--------|-------|
| Landing page copy reviewed | User | ☐ | Review pricing, tagline, links |
| README badges | Code | ✅ | CI badge already in `README.md` |
| Demo sample works end-to-end | Code | ✅ | `POST /v1/demo-keys` → parse `assets/samples/demo.pdf` |
