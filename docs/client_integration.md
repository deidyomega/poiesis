# Glitch Core — Client Integration Guide

This document describes how to build a client that communicates with Glitch Core. Clients are "dumb terminals" — they write user messages to Firestore and listen for agent responses in real-time. All AI processing happens on the Glitch Core daemon, never on the client.

## Architecture Overview

```
Client (your app) ←→ Firebase Firestore ←→ Glitch Core Daemon (processes messages)
```

- The client writes a message to Firestore
- The daemon detects it via real-time listener
- The daemon runs the AI agent and writes the response to Firestore
- The client sees the response appear via real-time listener
- No direct connection between client and daemon is needed
- The client only needs: Firebase project ID + auth credentials

## Firebase Setup

The client needs the Firebase SDK for your platform (JS, Flutter, Swift, Kotlin, etc.) configured with:

```
projectId: "your-firebase-project-id"
```

Authentication: Firebase Auth (email/password). The client signs in, then all Firestore operations are authenticated. (Note: auth is planned but not yet enforced — currently rules allow unauthenticated reads.)

## Firestore Collections

### Sessions — `/sessions/{session_id}`

Each conversation is a session tied to a specific agent.

```json
{
  "session_id": "s_a1b2c3d4",
  "agent_id": "router",
  "created_at": "2026-04-02T12:00:00Z"
}
```

- `session_id`: Unique ID. Generate as `s_{8_hex_chars}` or any unique string.
- `agent_id`: Which agent this session talks to. `"router"` is the default general-purpose agent. Other agents (e.g. `"coder"`, `"researcher"`) can be chatted with directly.

**To create a new session:** Write a document to `/sessions/{your_session_id}` with the fields above.

### Messages — `/sessions/{session_id}/messages/{message_id}`

The message bus. Both user and agent messages live here.

#### Writing a user message:

```json
{
  "message_id": "msg_a1b2c3d4e5f6",
  "session_id": "s_a1b2c3d4",
  "role": "user",
  "content": "Hello, what's the weather like?",
  "content_rating": "sfw",
  "attachments": [],
  "metadata": {},
  "created_at": "2026-04-02T12:00:01Z"
}
```

- `message_id`: Unique. Generate as `msg_{12_hex_chars}`.
- `role`: Always `"user"` for client-written messages.
- `content`: The message text (plain text or markdown).
- `content_rating`: `"sfw"` or `"nsfw"`.
- `created_at`: Timestamp. Use Firestore server timestamp or UTC ISO string.
- `attachments`: Reserved for future use. Send empty array.
- `metadata`: Reserved. Send empty object.

#### Listening for agent responses:

Subscribe to the messages subcollection with `orderBy("created_at")`. New messages appear in real-time. Agent messages have these roles:

| Role | Meaning |
|------|---------|
| `user` | Message from the user (you wrote this) |
| `agent` | Response from the AI agent |
| `sub_agent` | Response from a delegated sub-agent (e.g. coder, researcher) |
| `system` | System message (errors, notifications) |

#### Agent response format:

```json
{
  "message_id": "msg_f6e5d4c3b2a1",
  "session_id": "s_a1b2c3d4",
  "role": "agent",
  "content": "The weather in San Francisco is...",
  "content_rating": "sfw",
  "streaming": false,
  "thinking": false,
  "attachments": [],
  "metadata": {
    "agent_id": "router",
    "usage": {
      "input_tokens": 1500,
      "output_tokens": 200
    }
  },
  "created_at": "2026-04-02T12:00:03Z"
}
```

#### Streaming responses:

Agent responses stream in real-time. The daemon writes a placeholder, then updates it progressively:

1. **Placeholder appears:** `content: ""`, `streaming: true` — show a typing indicator
2. **Content updates:** `content` field grows every ~600ms — render progressively
3. **Tool execution:** `thinking: true` appears — show a "working..." indicator
4. **Final:** `streaming: false`, `thinking: false` — render the final content

Listen for both `added` and `modified` events on the messages subcollection:

```javascript
// JavaScript/Firebase example
onSnapshot(messagesQuery, (snapshot) => {
  snapshot.docChanges().forEach((change) => {
    if (change.type === 'added') {
      // New message — render it
    }
    if (change.type === 'modified') {
      // Streaming update — re-render the message bubble
    }
  });
});
```

For Flutter:
```dart
FirebaseFirestore.instance
  .collection('sessions').doc(sessionId)
  .collection('messages').orderBy('created_at')
  .snapshots()
  .listen((snapshot) {
    for (var change in snapshot.docChanges) {
      // change.type == DocumentChangeType.added
      // change.type == DocumentChangeType.modified
    }
  });
```

## Available Agents

Agents are configured in `/agents/{agent_id}`. To list available agents:

```
GET /agents/ → each doc has: agent_id, name, description, enabled, content_rating
```

The client can present a picker letting the user choose which agent to chat with. Create a session with `agent_id` set to the chosen agent.

## Content Rating

Messages and agents have a `content_rating` field: `"sfw"` or `"nsfw"`.

- SFW agents refuse NSFW content
- NSFW agents (e.g. local uncensored models) handle anything
- The client should set `content_rating: "sfw"` on user messages by default
- If the client is explicitly for NSFW use, set `content_rating: "nsfw"`

## Message Content Format

- `content` is **markdown** (GitHub-flavored). The client should render it accordingly.
- Code blocks use triple backticks with language hints.
- Tables use standard markdown table syntax.
- Sub-agent responses may include structured sections (bold headers, bullet lists, code).

## Notifications & Reminders

The daemon has a built-in reminder system. When a user says "remind me to check the laundry in 20 minutes," the agent creates a reminder that fires later. When it fires, a message appears in the session with a `notification` field. **The client is responsible for deciding how to alert the user.**

### The `notification` field

Regular messages have `notification: null`. Reminder messages (and future alert types) have:

```json
{
  "message_id": "msg_abc123",
  "role": "agent",
  "content": "Hey! Time to check the laundry! 🧺",
  "notification": {
    "type": "reminder",
    "sound": true,
    "title": "Reminder"
  },
  ...
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | The notification category. Currently `"reminder"`. Future types may include `"alert"`, `"task_complete"`, etc. |
| `sound` | bool | Whether the client should play an audible alert. |
| `title` | string | Short title for the notification (for system notification banners). |

The `content` field contains the full reminder text — pre-composed by the agent at the time the user set the reminder.

### Client implementation

When a new message arrives via `onSnapshot` with a `notification` field present:

1. **Check `notification.sound`** — if `true`, play an alert sound (client decides which sound)
2. **Fire a platform notification** — use the platform's native notification system:
   - **Browser:** `new Notification(title, { body: content })`
   - **Flutter/Mobile:** local notification plugin (e.g. `flutter_local_notifications`)
   - **Desktop companion:** system tray notification, sound, VRM animation, etc.
3. **Render the message normally** — it appears in chat like any other agent message

### Example: JavaScript (browser)

```javascript
onSnapshot(messagesQuery, (snapshot) => {
  snapshot.docChanges().forEach((change) => {
    if (change.type === 'added') {
      const data = change.doc.data();

      // Check for notification
      if (data.notification) {
        // Browser notification (requires permission)
        if ('Notification' in window && Notification.permission === 'granted') {
          new Notification(data.notification.title || 'Glitch', {
            body: (data.content || '').slice(0, 200),
          });
        }

        // Play sound (client decides the audio file)
        if (data.notification.sound) {
          const audio = new Audio('/sounds/notification.mp3');
          audio.play().catch(() => {}); // ignore autoplay restrictions
        }
      }

      // Render message as usual...
    }
  });
});
```

### Example: Flutter (mobile)

```dart
messagesStream.listen((snapshot) {
  for (var change in snapshot.docChanges) {
    if (change.type == DocumentChangeType.added) {
      final data = change.doc.data()!;
      final notification = data['notification'] as Map<String, dynamic>?;

      if (notification != null) {
        // Fire local notification
        FlutterLocalNotificationsPlugin().show(
          0,
          notification['title'] ?? 'Glitch',
          (data['content'] as String?)?.substring(0, 200) ?? '',
          NotificationDetails(
            android: AndroidNotificationDetails('reminders', 'Reminders'),
            iOS: DarwinNotificationDetails(),
          ),
        );
      }

      // Render message as usual...
    }
  }
});
```

### Example: Desktop companion (custom event)

The web UI dispatches a DOM custom event that a desktop companion app (running in an embedded browser or Electron) can listen to:

```javascript
window.addEventListener('glitch-notification', (event) => {
  const { type, title, body, message_id } = event.detail;
  // Play custom sound, trigger VRM dance, show system tray popup, etc.
});
```

### Notification types (current and planned)

| Type | When it fires | `sound` default |
|------|--------------|-----------------|
| `reminder` | User-requested reminder fires after delay | `true` |
| `task_complete` | (Planned) A long-running sub-agent task finishes | `true` |
| `alert` | (Planned) System alert (compaction failed, worker offline, etc.) | `true` |

Clients should handle unknown `type` values gracefully — treat them as generic notifications.

### Reminder Firestore schema

Reminders are stored in `/reminders/{reminder_id}`:

```json
{
  "reminder_id": "rem_abc123",
  "session_id": "s_d99918df",
  "agent_id": "router",
  "message": "Hey! Time to check the laundry! 🧺",
  "fire_at": "2026-04-03T15:30:00Z",
  "fired": false,
  "created_at": "2026-04-03T15:10:00Z"
}
```

The daemon checks every 15 seconds for unfired reminders past their `fire_at` time. When found, it writes the message to the session and sets `fired: true`. Reminders survive daemon restarts — they're in Firestore, not in-memory timers.

Clients do NOT need to read `/reminders/` — they only need to watch messages for the `notification` field. The reminders collection is an internal implementation detail.

## Minimal Client Implementation

A complete client needs to do exactly **5 things**:

1. **Create a session** — write one document to `/sessions/{id}`
2. **Write user messages** — write documents to `/sessions/{id}/messages/{msg_id}`
3. **Listen for responses** — `onSnapshot` on the messages subcollection
4. **Render streaming** — handle `streaming`, `thinking`, and `modified` events
5. **Handle notifications** — when a message has a `notification` field, alert the user (sound, system notification, etc.)

That's it. The daemon handles everything else — AI processing, memory, tool execution, sub-agent dispatch, reminders.

## What the Client Does NOT Do

- No AI/LLM calls — the daemon handles all model interaction
- No Firestore writes beyond sessions and user messages
- No direct connection to the daemon process
- No API key management (keys live on the daemon's server)
- No tool execution — tools run on the daemon/workers
- No memory management — compaction runs automatically on the daemon

## Example: Sending a Message and Getting a Response

```
1. Client writes to /sessions/s_abc123/messages/msg_001:
   { role: "user", content: "Hello!", created_at: now(), ... }

2. Daemon's on_snapshot fires, processes the message

3. Daemon writes to /sessions/s_abc123/messages/msg_002:
   { role: "agent", content: "", streaming: true, ... }

4. Daemon updates msg_002 every 600ms:
   { content: "Hi there! ", streaming: true }
   { content: "Hi there! How can I help ", streaming: true }
   { content: "Hi there! How can I help you today?", streaming: false }

5. Client renders each update progressively
```

## Latency

- Message write → daemon detection: ~100ms (Firestore on_snapshot)
- Daemon → first token: ~500ms-2s (depends on model)
- Streaming updates: every ~600ms
- Total time to first visible response: ~1-3 seconds
