import http from 'k6/http';
import { check, sleep } from 'k6';

// Authenticated, read-only school-workspace workload. Fixture setup is done
// beforehand through the public API; this scenario does not create, modify,
// send, upload, or invoke any AI endpoint.
const baseUrl = __ENV.BASE_URL || 'https://repetam.ru';
const token = __ENV.ACCESS_TOKEN;
if (!token) throw new Error('ACCESS_TOKEN must be supplied');

export const options = {
  stages: [
    { duration: '30s', target: 10 },
    { duration: '90s', target: 25 },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<500'],
    checks: ['rate>0.99'],
  },
};

const paths = [
  '/api/v1/online-schools/me/dashboard/overview?period=week',
  '/api/v1/online-schools/me/tutors',
  '/api/v1/online-schools/me/students?status=active',
  '/api/v1/online-schools/me/student-groups',
  '/api/v1/online-schools/me/schedule?from=2026-09-15T00:00:00Z&to=2026-09-30T00:00:00Z',
  '/api/v1/online-schools/me/assignments?page=1&page_size=50',
  '/api/v1/online-schools/me/analytics/overview?period=last_30_days&comparison=true&students_page_size=50',
];

export default function () {
  const response = http.get(`${baseUrl}${paths[__ITER % paths.length]}`, {
    headers: { Authorization: `Bearer ${token}` },
    tags: { scenario: 'school-workspace-read' },
  });
  check(response, { 'workspace response is 200': (r) => r.status === 200 });
  sleep(1);
}
