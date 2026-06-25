"""Anonymous push-relay: device registrations + APNs/FCM notify via the relay.

Gateway bots can't send APNs themselves (the app's APNs key lives only on the
relay), so out-of-band notifications (cron results while the app is closed) are
forwarded through the relay's ``/api/push/send`` endpoint, keyed by an opaque
``pushId`` the device handed us. No Flowly account required.
"""
