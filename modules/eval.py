from modules.common.module import BotModule
from nio import RoomMessageText

import shlex
from bs4 import BeautifulSoup
from html import escape

from subprocess import run, PIPE

class MatrixModule(BotModule):
    def __init__(self, name):
        super().__init__(name)
        self.langmap  = dict()
        self.aliases  = dict()
        self.commands = {
                **dict.fromkeys(['add', 'new', 'addlang', 'newlang'], self.add_lang),
                **dict.fromkeys(['rm', 'remove', 'rmlang', 'delete'], self.rm_lang),
                **dict.fromkeys(['alias', 'aliaslang'], self.alias_lang),
                **dict.fromkeys(['set', 'setlang', 'setprop'], self.set_lang_prop),
                **dict.fromkeys(['get', 'getlang', 'getprop'], self.get_lang_prop),
                **dict.fromkeys(['list', 'ls', 'langs'], self.list_langs),
                **dict.fromkeys(['run'], self.run_code),
        }

    def set_settings(self, data):
        super().set_settings(data)
        if data.get('langmap'):
            self.langmap = data['langmap']
        if data.get('aliases'):
            self.aliases = data['aliases']

    def get_settings(self):
        data = super().get_settings()
        data['langmap'] = self.langmap
        data['aliases'] = self.aliases
        return data

    def matrix_start(self, bot):
        super().matrix_start(bot)
        self.bot = bot
        bot.client.add_event_callback(self.message_cb, RoomMessageText)
        langs = [*self.aliases.keys(), *self.langmap.keys()]
        self.add_module_aliases(bot, langs + [f'eval{key}' for key in langs])

    def matrix_stop(self, bot):
        super().matrix_stop(bot)
        bot.remove_callback(self.message_cb)

    async def add_lang(self, bot, room, event, cmd):
        bot.must_be_owner(event)
        self.logger.info(f"sender: {event.sender} wants to add a language")
        args = shlex.split(event.body)

        key = args[0].lower()
        if len(args) < 3:
            return {'send_text': f'{cmd} needs at least three arguments'}

        if key in self.langmap.keys():
            return {'send_text': f'{args[0]} already exists'}

        self.logger.info(f"sender: {event.sender} is adding a language")
        self.langmap[key] = {"container": args[1], "command": args[2:]}
        self.add_module_aliases(bot, [key, f'eval{key}'])
        bot.save_settings()
        await bot.send_text(room, f'Added {args[0]}')

    async def rm_lang(self, bot, room, event, cmd):
        bot.must_be_owner(event)
        self.logger.info(f"sender: {event.sender} wants to remove a language")
        args = event.body.split()

        if len(args) != 1:
            return {'send_text': f'{cmd} takes exactly one arguments'}

        try:
            self.langmap.pop(args[0])
            self.aliases = {k:v for k, v in self.aliases.items() if v != args[0]}
            bot.save_settings()
            await bot.send_text(room, f'removed language {args[0]}')
            return
        except KeyError:
            pass
        try:
            self.aliases.pop(args[0])
            bot.save_settings()
            await bot.send_text(room, f'removed alias {args[0]}')
        except:
            await bot.send_text(room, f'No language or alias found')

    async def list_langs(self, bot, room, event, cmd):
        ret = []
        for name in self.langmap.keys():
            aliases = [k for k, v in self.aliases.items() if v == name]
            if aliases:
                name += ' ({})'.format(' '.join(aliases))
            ret.append(name)
        await bot.send_text(room, '\n'.join(ret))

    async def alias_lang(self, bot, room, event, cmd):
        bot.must_be_owner(event)
        self.logger.info(f"sender: {event.sender} wants to alias a language")
        args = event.body.split()

        if len(args) < 2:
            msg = f'{cmd} takes two arguments'

        args = [arg.lower() for arg in args]
        if args[0] in self.langmap.keys():
            msg = f'Already a language: {args[0]}'

        if not args[1] in self.langmap.keys():
            msg = f'Not a language: {args[1]}'

        self.aliases[args[0]] = args[1]
        self.add_module_aliases(bot, [args[0], f'eval{args[0]}'])
        return {'send_text': f'Added {args[0]}', 'save_settings': True}

    async def set_lang_prop(self, bot, room, event, cmd):
        bot.must_be_owner(event)
        self.logger.info(f"sender: {event.sender} wants to modify a language")
        args = shlex.split(event.body)

        if len(args) < 3:
            return {'send_text': f'Usage: {cmd} [lang] [property] [value ...].'}
        lang = self.get_lang(args[0])
        if not lang:
            return {'send_text': f'{lang} has not been added.'}

        # integer values
        if args[1] in ['timeout']:
            val = int(args[2])
            if val <= 0:
                return {'send_text', f'{args[1]} must be a positive integer'}
        # string values
        elif args[1] in ['container', 'memory', 'pids', 'net', 'workdir']:
            val = args[2]
        # list values
        elif args[1] in ['podman_opts', 'command']:
            val = args[2:]
        # unknown values
        else:
            return {'send_text': f'Not a property: {args[1]}'}

        lang[args[1]] = val
        return {'send_text': f'Set property {args[1]} for {args[0]}', 'save_settings': True}

    async def get_lang_prop(self, bot, room, event, cmd):
        self.logger.info(f"sender: {event.sender} wants to list a language's properties")
        lang = self.get_lang(event.body.split(None, 1)[0])
        if not lang:
            msg = f'{lang} has not been added.'
        else:
            msg = [f'{args[0]}:']
            for key, val in lang.items():
                msg.append(f'- {key}: {val}')
            msg = '\n'.join(msg)
        await bot.send_text(room, msg)

    async def run_code(self, bot, room, event, cmd):
        self.logger.info(f"sender: {event.sender} wants to eval some code")
        lang, code = self.get_code(cmd, event)
        container = lang['container']
        podman_cmd = lang['command']
        self.logger.info(f"Running in podman {container} with {podman_cmd}")
        podman_opts = [f'--label={cmd}-{event.sender}']

        # set limits
        timeout = lang.get('timeout') or 15
        net = lang.get('net') or 'none'
        pids = lang.get('pids-limit') or 64
        mem = lang.get('memory') or '32M'
        workdir = lang.get('workdir') or '/'

        podman_opts += [f'--pids-limit={pids}', f'--memory={mem}', f'--net={net}', f'--workdir={workdir}']
        podman_opts += lang.get('podman_opts') or []

        proc = run(['podman', 'run', '--rm', '-i'] + podman_opts + [container] + podman_cmd,
                input=code.encode('utf-8'), stdout=PIPE, stderr=PIPE, timeout=timeout)
        parts = [self.code_block('stdout', proc.stdout.decode().strip('\n')), self.code_block('stderr', proc.stderr.decode().strip('\n'))]
        if proc.returncode != 0:
            parts.insert(0, (f'<p><strong>Process exited non-zero</strong>: <code>{proc.returncode}</code></p>',
                    f'(Process exited non-zero: {proc.returncode})'))

        html, plain = ('\n'.join(i) for i in zip(*parts))
        await bot.send_html(room, html, plain)

    async def message_cb(self, room, event):
        """
        Handle client callbacks for all room text events
        """
        if self.bot.should_ignore_event(event):
            return

        # no content at all?
        if len(event.body) < 1:
            return

        if "content" in event.source:
            # skip edited content to prevent spamming the same thing multiple times
            if "m.new_content" in event.source["content"]:
                self.logger.debug("Skipping edited event to prevent spam")
                return
            # skip reply messages to prevent spam
            if "m.relates_to" in event.source["content"]:
                self.logger.debug("Skipping reply message to prevent spam")
                return
        # TODO
        return

    async def matrix_message(self, bot, room, event):
        try:
            cmd, event.body = event.body.split(None, 1)      # [!cmd] [(!)subcmd body]
            if cmd in ['!' + self.name, self.name]:
                cmd, event.body = event.body.split(None, 1)  # [!subcmd] [body]
        except ValueError:
            # couldn't split, not enough arguments in body
            cmd = event.body.strip()
            event.body = ''
        cmd = cmd.lstrip('!')

        op = self.commands.get(cmd) or self.run_code
        await op(bot, room, event, cmd)

    def get_code(self, cmd, event):
        lang = None
        try:
            blocks = BeautifulSoup(event.formatted_body, features='html.parser').find_all('code')
            for block in blocks:
                c = block.get('class')
                if not c:
                    continue
                lang = self.get_lang(c[0])
                if lang:
                    break
            else:
                block = blocks[0]
            return (lang or self.get_lang(cmd), block.contents[0].string)
        except (AttributeError, IndexError):
            # No formatted_body or no <code> block, use event.body instead
            return (self.get_lang(cmd), event.body)

    def get_lang(self, s):
        # Python 3.9
        s = s.removeprefix('language-')
        s = self.aliases.get(s) or s
        return self.langmap.get(s)

    def code_block(self, header, text):
        if text:
            return (
                f'<p><strong>{escape(header)}: </strong></p><pre><code class="language-txt">'
                + escape(text) + '</code></pre>',
                # use markdown-style blocks for clients which parse it from event.body
                '\n'.join([header, '```', text.rstrip(), '```'])
            )
        else:
            return (f'<p><em>no {escape(header)}</em>', f'(no {header})')

    def help(self):
        return 'Evaluate code in a container'


    def long_help(self, bot=None, event=None, **kwargs):
        text = self.help() + (
                '\n- !eval (list|ls|langs): list the current languages and their aliases'
                '\n- !eval (get|getprop) [lang]: get the current properties for the given language'
                '\n- !eval run [lang] [code]: run code (see below)'
                '\n- !eval[lang] [code]: run code (see below)'
                '\n- ![lang] [code]: run code (see below)'
                )
        if bot and event and bot.is_owner(event):
            text += ('\n- !eval (add|new) [lang] [container] [command ...]: add an new language'
                     '\n- !eval alias [name] [lang]: add an alias [name] for [lang]'
                     '\n- !eval (set|setprop) [lang] [property] [value]: alter a language'
                     '\n- !eval (remove|rm) [name]: remove a language, or alias to a language'
                     )
        text += ('\nRunning code:'
                 '\n    !eval will attempt to find a code block in the message\'s formatteed_body.'
                 '\n    If a multiline codeblock has a language specifier, that language is preferred.'
                 '\n    Otherwise, the first codeblock (inline or otherwise) is used'
                 'with the language given in one of the forms above.'
                 '\n    If no codeblock is found, then the remaining body after [lang] is interpreted as code.'
                 )
        return text
