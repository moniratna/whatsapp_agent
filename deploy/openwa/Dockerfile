FROM openwa/wa-automate:latest@sha256:d31f5a8f59c4890a302294e9834a25f217a973d80339896ef74cf3d35d903cb3

WORKDIR /usr/src/app/transport
COPY --chown=owauser:owauser package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --chown=owauser:owauser wa.config.mjs ./

CMD ["npm", "start"]
