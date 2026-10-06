FROM rasa/rasa:3.6.20-full

WORKDIR /app
COPY . /app

USER root
RUN pip install --no-cache-dir -r requirements.txt || true

EXPOSE 5005
ENTRYPOINT ["rasa"]
CMD ["run", "--enable-api", "--cors", "*", "--port", "5005"]
