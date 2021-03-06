# robotframework-tools
#
# Python Tools for Robot Framework and Test Libraries.
#
# Copyright (C) 2013-2016 Stefan Zimmermann <zimmermann.code@gmail.com>
#
# robotframework-tools is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# robotframework-tools is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with robotframework-tools. If not, see <http://www.gnu.org/licenses/>.

"""robottools.library.keywords

Everything related to testlibrary's Keyword handling.

.. moduleauthor:: Stefan Zimmermann <zimmermann.code@gmail.com>
"""
__all__ = ['Keyword',
  # from .utils:
  'KeywordName', 'KeywordsDict',
  # from .deco:
  'KeywordDecoratorType',
  # from .errors:
  'InvalidKeywordOption', 'KeywordNotDefined']

import sys
from six import reraise
from itertools import chain
from textwrap import dedent

from moretools import dictitems, dictvalues

from .errors import InvalidKeywordOption, KeywordNotDefined
from .utils import KeywordName, KeywordsDict
from .deco import KeywordDecoratorType


class Keyword(object):
    """The Keyword handler for Test Library instances.

    * Provides inspection of Keyword name, arguments, and documentation.
    * Instances get called by Test Libraries' ``.run_keyword()`` method.
    """
    def __init__(self, name, func, libinstance):
        """Initialize with Keyword's display `name`,
           the actual Keyword `func` and the Test Library instance.
        """
        self.name = name
        self.func = func
        self.libinstance = libinstance
        # Get all ContextHandler-derived classes for which this Keyword
        # has context-specific implementations,
        # to implicitly provide additional context_name= switching kwargs:
        self.context_handlers = set(ctx.handler for ctx in func.contexts)

    @property
    def __doc__(self):
        """The Keyword's documentation string,
        taken from ``self.func.__doc__``.
        """
        doc = self.func.__doc__
        if doc is None:
            return None
        if '\n' in doc:
            first_line, rest = doc.split('\n', 1)
            return "%s\n%s" % (first_line, dedent(rest))

    @property
    def libname(self):
        """The Keyword's Test Library class name,
        which also represents the Library's display name.
        """
        return type(self.libinstance).__name__

    @property
    def longname(self):
        """The Keyword's full display name (**TestLibraryName.Keyword Name**).
        """
        return '%s.%s' % (self.libname, self.name)

    def args(self):
        """Iterate the Keyword's argument spec in Robot's Dynamic API style,
           usable by Test Libraries' ``.get_keyword_arguments()`` method.
        """
        # First look for custom override args list:
        if self.func.args:
            for arg in self.func.args:
                yield arg
            return
        # Then fall back to the Keyword function's implicit argspec
        #  generated by Test Library's @keyword decorator:
        argspec = self.func.argspec
        posargs = argspec.args[1:]
        defaults = argspec.defaults
        if defaults:
            for arg, defaults_index in zip(
              posargs, range(-len(posargs), 0)
              ):
                try:
                    default = defaults[defaults_index]
                except IndexError:
                    yield arg
                else:
                    yield '%s=%s' % (arg, default)
        else:
            for arg in posargs:
                yield arg
        if argspec.varargs:
            yield '*' + argspec.varargs
        if argspec.keywords:
            yield '**' + argspec.keywords
        # if the Library has any session handlers or context handlers
        # with activated auto_explicit option
        # then always provide **kwargs
        # to support explicit <session>= and <context>= switching
        # for single Keyword calls:
        elif any(hcls.meta.auto_explicit
                 for hcls in dictvalues(self.libinstance.session_handlers)) \
          or any(getattr(hcls, 'auto_explicit', False)
                 for hcls in self.context_handlers):
            yield '**options'

    def __call__(self, *args, **kwargs):
        """Call the Keyword's actual function with the given arguments.
        """
        func = self.func
        # the exception to finally reraise (if any)
        error = None
        # look for explicit <session>= and <context>= switching options
        # in kwargs and store the currently active
        # session aliases and context names
        # for switching back after the Keyword call:
        current_sessions = {}
        for _, hcls in self.libinstance.session_handlers:
            if not hcls.meta.auto_explicit:
                continue
            identifier = hcls.meta.identifier_name
            plural_identifier = hcls.meta.plural_identifier_name
            try:
                sname = kwargs.pop(identifier)
            except KeyError:
                continue
            previous = getattr(self.libinstance, identifier)
            switch = getattr(self.libinstance, 'switch_' + identifier)
            try:
                switch(sname)
            except hcls.SessionError:
                error = sys.exc_info()
                # don't switch any more sessions
                break
            # store previous session for switching back later
            current_sessions[identifier, plural_identifier] = previous
        # only perform explicit context switching
        # if explicit session switching didn't raise any error
        current_contexts = {}
        if error is None:
            for hcls in self.context_handlers:
                if not getattr(hcls, 'auto_explicit', False):
                    continue
                identifier = hcls.__name__.lower()
                try:
                    ctxname = kwargs.pop(identifier)
                except KeyError:
                    continue
                previous = getattr(self.libinstance, identifier)
                switch = getattr(self.libinstance, 'switch_' + identifier)
                try:
                    switch(ctxname)
                except hcls.ContextError:
                    error = sys.exc_info()
                    # don't switch any more contexts
                    break
                # store previous context for switching back later
                current_contexts[identifier] = previous
        # only call the acutal keyword func
        # if explicit session and context switching didn't raise any error
        if error is None:
            # Look for arg type specs:
            if func.argtypes:
                casted = []
                for arg, argtype in zip(args, func.argtypes):
                    if not isinstance(arg, argtype):
                        arg = argtype(arg)
                    casted.append(arg)
                args = tuple(casted) + args[len(func.argtypes):]
            # Look for context specific implementation of the Keyword function
            for context, context_func in dictitems(func.contexts):
                if context in self.libinstance.contexts:
                    func = context_func
            # Does the keyword support **kwargs?
            if self.func.argspec.keywords or not kwargs:
                try:
                    result = func(self.libinstance, *args, **kwargs)
                except Exception:
                    error = sys.exc_info()
            else:
                # resolve **kwargs to positional args...
                posargs = []
                # (argspec.args start index includes self)
                for name in self.func.argspec.args[1 + len(args):]:
                    if name in kwargs:
                        posargs.append(kwargs.pop(name))
                # and turn the rest into *varargs in 'key=value' style
                varargs = ['%s=%s' % (key, kwargs.pop(key))
                           for key in list(kwargs)
                           if key not in self.func.argspec.args]
                try:
                    result = func(self.libinstance,
                                  *chain(args, posargs, varargs),
                                  # if **kwargs left ==> TypeError from Python
                                  **kwargs)
                except:
                    error = sys.exc_info()
        # finally try to switch back contexts and sessions (in reverse order)
        # before either returning result or reraising any error catched above
        for identifier, ctxname in dictitems(current_contexts):
            switch = getattr(self.libinstance, 'switch_' + identifier)
            # don't catch anything here. just step out on error
            switch(ctxname)
        for (identifier, plural_identifier), session in dictitems(
                current_sessions
        ):
            for sname, sinstance in dictitems(getattr(
                    self.libinstance, plural_identifier
            )):
                if sinstance is session:
                    switch = getattr(self.libinstance, 'switch_' + identifier)
                    # don't catch anything here. just step out on error
                    switch(sname)
        # was an error catched on initial session or context switching
        # or on calling the actual keyword func?
        if error is not None:
            reraise(*error)
        # great! everything went fine :)
        return result

    def __repr__(self):
        return '%s [ %s ]' % (self.longname, ' | '.join(self.args()))
