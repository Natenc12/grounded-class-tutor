# Vision — where Grounded Class Tutor is going

**Settled by the owner on 2026-11-27.** This page is the finish line: what the app is when it is
done. It changes rarely, and only by the owner's decision. The route to it changes often and lives
elsewhere: [#216](https://github.com/Natenc12/grounded-class-tutor/issues/216) for the current
build sequence, and the board for what is ready to pick up.

The first proposal, written for the hosted web app, is kept as history in
[`first-proposal.md`](first-proposal.md).

## Why this project exists

The owner is building this to learn what it takes to build a real application from start to
finish, and to finish one. It does not have to earn its place as a study tool.

So the goal is a small app that is complete and well made, not an app that keeps growing. When
there is a choice between adding something and finishing, finish. The quality bar set in #216
stays: every change has its own ticket, one reviewed pull request and a green merge check.

## The promise

A Mac app I open from the Dock. I drop my course files into a class, ask a question, and get an
answer that points to the exact page or slide it came from, or an honest "that's not in your
materials." Everything lives on my Mac and runs on my own Claude login.

Trust is still the product. An answer is either cited to the source or it is a plain refusal;
the app never fills a gap with a guess.

## Finished means

The app is finished when every line below is true on the owner's Mac, with his own course files.

1. **It opens like any Mac app.** No Terminal is needed to start it or to use it.
2. **I can make a class and drag files into it.** PDF and PowerPoint files, and I can see them
   being imported.
3. **I ask in a chat window.** The answer appears with numbered citations.
4. **Clicking a citation shows the source.** The page or slide it came from, with the passage
   marked.
5. **It says so when the answer isn't in my files.** It does not guess.
6. **Past conversations are saved per class.** I can come back to them later, and a follow-up
   question remembers the ones before it: after "What is base flow?", "How is it measured?"
   is understood (decided 2026-11-27).
7. **Search finds passages by meaning.** Not only by matching words, and it uses a free model
   that runs on the Mac.
8. **One command builds the app from the repo.**

## It will never have

- Sign-in or accounts.
- Hosting, sharing, or more than one user.
- A phone version.
- Quizzes, flashcards or study guides.
- Answers from any model other than Claude. (The free stand-in that tests use, and the local
  search model in line 7, are not answer providers and stay.)

These are not "later". They are out, so that the list above can be reached and the work can
stop.

## Using this page to plan work

- Work that moves one of the eight lines forward is in scope. A ticket should say which line.
- Work that keeps what exists working is in scope: bugs, safety updates, tests and upkeep.
- Anything else needs the owner's say-so before it becomes a ticket. That includes anything on
  the "never" list, however small.
- When all eight lines are true, the app is finished. After that, only fixes.

## Not yet decided

This is the owner's call. Do not guess it.

- **Line 4:** whether the source is shown as a picture of the real page or slide, or as the
  page's text with the passage marked. The owner will choose after trying a prototype that shows
  both on real course files (decided 2026-11-27).
