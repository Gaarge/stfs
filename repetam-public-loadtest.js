import http from 'k6/http';
import { check, sleep } from 'k6';

// Deliberately read-only: this script never signs in, creates records, sends
// mail, uploads files, or calls AI. Run it from the production host so its
// traffic is measured by the Caddy/Prometheus capacity dashboard.
const baseUrl = __ENV.BASE_URL || 'https://repetam.ru';

export const options = {
  stages: [
    { duration: '15s', target: 2 },
    { duration: '30s', target: 10 },
    { duration: '15s', target: 0 },
  ],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<500'],
  },
};

export default function () {
  const path = __ITER % 5 === 0 ? '/promo/' : '/';
  const response = http.get(`${baseUrl}${path}`, {
    redirects: 3,
    tags: { scenario: 'public-read' },
  });
  check(response, {
    'final response is 200': (r) => r.status === 200,
  });
  sleep(1);
}
