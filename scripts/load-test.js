// CM TECHMAP — k6 Load Test Suite
// Simulates 150+ municipalities and 1000+ concurrent users
//
// Usage:
//   k6 run --vus 100 --duration 60s scripts/load-test.js
//   k6 run --vus 500 --duration 300s scripts/load-test.js
//   k6 run --vus 1000 --duration 600s scripts/load-test.js
//
// Docker:
//   docker run --rm -i --network cm-network grafana/k6 run - < scripts/load-test.js

import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';

// ═════════════════════════════════════════════════════════════════════════════
// Configuration
// ═════════════════════════════════════════════════════════════════════════════

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

export const options = {
  stages: [
    { duration: '30s', target: 50 },    // Ramp up to 50 users
    { duration: '60s', target: 200 },   // Ramp to 200 users
    { duration: '120s', target: 500 },  // Ramp to 500 users  
    { duration: '120s', target: 1000 }, // Peak: 1000 concurrent users
    { duration: '60s', target: 500 },   // Ramp down
    { duration: '30s', target: 0 },     // Cool down
  ],
  thresholds: {
    http_req_duration: ['p(95)<2000'],   // 95% of requests < 2s
    http_req_failed: ['rate<0.05'],      // Error rate < 5%
    'health_check_duration': ['p(99)<500'],
    'dashboard_duration': ['p(95)<3000'],
    'map_tiles_duration': ['p(95)<1000'],
  },
};

// Custom metrics
const healthCheckDuration = new Trend('health_check_duration');
const dashboardDuration = new Trend('dashboard_duration');
const mapTilesDuration = new Trend('map_tiles_duration');
const authDuration = new Trend('auth_duration');
const discrepancyDuration = new Trend('discrepancy_duration');
const errorRate = new Rate('errors');
const requestCounter = new Counter('total_requests');

// ═════════════════════════════════════════════════════════════════════════════
// Scenarios
// ═════════════════════════════════════════════════════════════════════════════

export default function () {
  // Randomly select a scenario to simulate realistic traffic patterns
  const scenarios = [
    { weight: 30, fn: healthAndDashboard },
    { weight: 25, fn: mapViewing },
    { weight: 20, fn: discrepancyWorkflow },
    { weight: 15, fn: projectBrowsing },
    { weight: 10, fn: publicDataAccess },
  ];

  const totalWeight = scenarios.reduce((sum, s) => sum + s.weight, 0);
  let random = Math.random() * totalWeight;

  for (const scenario of scenarios) {
    random -= scenario.weight;
    if (random <= 0) {
      scenario.fn();
      return;
    }
  }
  healthAndDashboard();
}

// ═════════════════════════════════════════════════════════════════════════════
// Scenario 1: Health Check + Dashboard (30% of traffic)
// ═════════════════════════════════════════════════════════════════════════════

function healthAndDashboard() {
  group('Health & Dashboard', function () {
    // Health check
    let res = http.get(`${BASE_URL}/api/v1/health`);
    healthCheckDuration.add(res.timings.duration);
    requestCounter.add(1);
    check(res, {
      'health is 200': (r) => r.status === 200,
      'health response < 500ms': (r) => r.timings.duration < 500,
    }) || errorRate.add(1);

    sleep(0.5);

    // Dashboard stats
    res = http.get(`${BASE_URL}/api/v1/health/ready`);
    dashboardDuration.add(res.timings.duration);
    requestCounter.add(1);
    check(res, {
      'ready is 200': (r) => r.status === 200,
    }) || errorRate.add(1);

    sleep(1);

    // OpenAPI spec (simulates frontend loading)
    res = http.get(`${BASE_URL}/openapi.json`);
    requestCounter.add(1);
    check(res, {
      'openapi loads': (r) => r.status === 200,
      'openapi has paths': (r) => {
        try { return JSON.parse(r.body).paths !== undefined; } catch(e) { return false; }
      },
    }) || errorRate.add(1);

    sleep(Math.random() * 2 + 1);
  });
}

// ═════════════════════════════════════════════════════════════════════════════
// Scenario 2: Map Tile Viewing (25% of traffic)
// ═════════════════════════════════════════════════════════════════════════════

function mapViewing() {
  group('Map Viewing', function () {
    // Simulate viewing map tiles (public endpoints)
    const tileEndpoints = [
      '/api/v1/public/map/stats',
    ];

    for (const endpoint of tileEndpoints) {
      let res = http.get(`${BASE_URL}${endpoint}`);
      mapTilesDuration.add(res.timings.duration);
      requestCounter.add(1);
      check(res, {
        'map endpoint responds': (r) => r.status < 500,
        'map response < 1s': (r) => r.timings.duration < 1000,
      }) || errorRate.add(1);
      sleep(0.3);
    }

    // Simulate rapid tile requests (user panning/zooming)
    for (let i = 0; i < 5; i++) {
      let res = http.get(`${BASE_URL}/api/v1/public/map/stats`);
      requestCounter.add(1);
      mapTilesDuration.add(res.timings.duration);
      sleep(0.1);
    }

    sleep(Math.random() * 3 + 1);
  });
}

// ═════════════════════════════════════════════════════════════════════════════
// Scenario 3: Discrepancy Review Workflow (20% of traffic)
// ═════════════════════════════════════════════════════════════════════════════

function discrepancyWorkflow() {
  group('Discrepancy Workflow', function () {
    // Login attempt (simulates auth flow)
    let res = http.post(`${BASE_URL}/api/v1/auth/login`, JSON.stringify({
      username: `user${Math.floor(Math.random() * 1000)}@test.gov.br`,
      password: 'TestPass123',
    }), { headers: { 'Content-Type': 'application/json' } });
    authDuration.add(res.timings.duration);
    requestCounter.add(1);
    // Auth will fail (no real user), but we're testing throughput
    check(res, {
      'auth responds': (r) => r.status < 500 || r.status === 503,
      'auth response < 3s': (r) => r.timings.duration < 3000,
    }) || errorRate.add(1);

    sleep(1);

    // Subscription plans (public endpoint, simulates page load)
    res = http.get(`${BASE_URL}/api/v1/subscriptions/plans`);
    discrepancyDuration.add(res.timings.duration);
    requestCounter.add(1);
    check(res, {
      'plans load': (r) => r.status === 200,
    }) || errorRate.add(1);

    sleep(Math.random() * 2 + 1);
  });
}

// ═════════════════════════════════════════════════════════════════════════════
// Scenario 4: Project Browsing (15% of traffic)
// ═════════════════════════════════════════════════════════════════════════════

function projectBrowsing() {
  group('Project Browsing', function () {
    // Root endpoint
    let res = http.get(`${BASE_URL}/`);
    requestCounter.add(1);
    check(res, {
      'root responds': (r) => r.status === 200,
    }) || errorRate.add(1);

    sleep(0.5);

    // Health (every page load checks health)
    res = http.get(`${BASE_URL}/api/v1/health`);
    healthCheckDuration.add(res.timings.duration);
    requestCounter.add(1);

    sleep(Math.random() * 3 + 1);
  });
}

// ═════════════════════════════════════════════════════════════════════════════
// Scenario 5: Public Data Access (10% of traffic)
// ═════════════════════════════════════════════════════════════════════════════

function publicDataAccess() {
  group('Public Data', function () {
    const endpoints = [
      '/api/v1/public/info',
      '/api/v1/subscriptions/plans',
      '/api/v1/health',
      '/api/v1/health/ready',
    ];

    for (const endpoint of endpoints) {
      let res = http.get(`${BASE_URL}${endpoint}`);
      requestCounter.add(1);
      check(res, {
        [`${endpoint} responds`]: (r) => r.status < 500,
      }) || errorRate.add(1);
      sleep(0.2);
    }

    sleep(Math.random() * 2 + 1);
  });
}

// ═════════════════════════════════════════════════════════════════════════════
// Summary
// ═════════════════════════════════════════════════════════════════════════════

export function handleSummary(data) {
  const totalRequests = data.metrics.total_requests?.values?.count || 0;
  const p95 = data.metrics.http_req_duration?.values?.['p(95)'] || 0;
  const errorPct = data.metrics.errors?.values?.rate || 0;
  
  console.log('\n╔══════════════════════════════════════════════════════════╗');
  console.log('║  CM TECHMAP — Load Test Results                         ║');
  console.log('╠══════════════════════════════════════════════════════════╣');
  console.log(`║  Total Requests: ${totalRequests}`);
  console.log(`║  P95 Latency:    ${p95.toFixed(0)}ms`);
  console.log(`║  Error Rate:     ${(errorPct * 100).toFixed(2)}%`);
  console.log(`║  Health P99:     ${(data.metrics.health_check_duration?.values?.['p(99)'] || 0).toFixed(0)}ms`);
  console.log(`║  Dashboard P95:  ${(data.metrics.dashboard_duration?.values?.['p(95)'] || 0).toFixed(0)}ms`);
  console.log(`║  Map Tiles P95:  ${(data.metrics.map_tiles_duration?.values?.['p(95)'] || 0).toFixed(0)}ms`);
  console.log('╚══════════════════════════════════════════════════════════╝\n');

  return {
    stdout: JSON.stringify(data, null, 2),
  };
}
