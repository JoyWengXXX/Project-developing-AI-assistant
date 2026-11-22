import express from 'express'

export const app = express()

app.get('/health', (_req, res) => {
  res.json({ status: 'ok' })
})

if (require.main === module) {
  app.listen(3000)
}
