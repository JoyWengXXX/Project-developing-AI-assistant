const express = require('express')

const app = express()

app.get('/health', (_req, res) => {
  res.json({ status: 'ok' })
})

if (require.main === module) {
  app.listen(3000)
}

module.exports = { app }
