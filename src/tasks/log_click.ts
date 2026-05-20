import type { Task } from 'graphile-worker';
import { pool } from '../db.ts';

interface Payload {
  urlId: string;
  ip: string | null;
  userAgent: string | null;
}

const log_click: Task = async (payload, helpers) => {
  const { urlId, ip, userAgent } = payload as Payload;

  // Simulated 200ms geo-IP lookup — stands in for a real third-party call.
  await new Promise((r) => setTimeout(r, 200));
  const country = 'US';

  await pool.query(
    'INSERT INTO clicks (url_id, ip, user_agent, country) VALUES ($1, $2, $3, $4)',
    [urlId, ip, userAgent, country],
  );

  helpers.logger.info(`logged click url_id=${urlId} country=${country}`);
};

export default log_click;
