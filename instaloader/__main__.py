"""Download pictures (or videos) along with their captions and other metadata from Instagram."""

import ast
import datetime
import os
import sys
from argparse import ArgumentParser, SUPPRESS
from typing import List, Optional

from . import (Instaloader, InstaloaderException, InvalidArgumentException, Post, Profile, ProfileNotExistsException,
               StoryItem, __version__, load_structure_from_file)
from .instaloader import get_default_session_filename
from .instaloadercontext import default_user_agent


def usage_string():
    # NOTE: duplicated in README.rst and docs/index.rst
    argv0 = os.path.basename(sys.argv[0])
    argv0 = "instaloader" if argv0 == "__main__.py" else argv0
    return """
{0} [--comments] [--geotags] [--stories]
{2:{1}} [--login YOUR-USERNAME] [--fast-update]
{2:{1}} profile | "#hashtag" | :stories | :feed | :saved
{0} --help""".format(argv0, len(argv0), '')


def filterstr_to_filterfunc(filter_str: str, item_type: type):
    """Takes an --post-filter=... or --storyitem-filter=... filter
     specification and makes a filter_func Callable out of it."""

    # The filter_str is parsed, then all names occurring in its AST are replaced by loads to post.<name>. A
    # function Post->bool is returned which evaluates the filter with the post as 'post' in its namespace.

    class TransformFilterAst(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name):
            # pylint:disable=no-self-use
            if not isinstance(node.ctx, ast.Load):
                raise InvalidArgumentException("Invalid filter: Modifying variables ({}) not allowed.".format(node.id))
            if node.id == "datetime":
                return node
            if not hasattr(item_type, node.id):
                raise InvalidArgumentException("Invalid filter: {} not a {} attribute.".format(node.id,
                                                                                               item_type.__name__))
            new_node = ast.Attribute(ast.copy_location(ast.Name('item', ast.Load()), node), node.id,
                                     ast.copy_location(ast.Load(), node))
            return ast.copy_location(new_node, node)

    input_filename = '<command line filter parameter>'
    compiled_filter = compile(TransformFilterAst().visit(ast.parse(filter_str, filename=input_filename, mode='eval')),
                              filename=input_filename, mode='eval')

    def filterfunc(item) -> bool:
        # pylint:disable=eval-used
        return bool(eval(compiled_filter, {'item': item, 'datetime': datetime.datetime}))

    return filterfunc


def _main(instaloader: Instaloader, targetlist: List[str],
          username: Optional[str] = None, password: Optional[str] = None,
          sessionfile: Optional[str] = None, max_count: Optional[int] = None,
          profile_pic: bool = True, profile_pic_only: bool = False,
          fast_update: bool = False,
          stories: bool = False, stories_only: bool = False,
          post_filter_str: Optional[str] = None,
          storyitem_filter_str: Optional[str] = None) -> None:
    """Download set of profiles, hashtags etc. and handle logging in and session files if desired."""
    # Parse and generate filter function
    post_filter = None
    if post_filter_str is not None:
        post_filter = filterstr_to_filterfunc(post_filter_str, Post)
        instaloader.context.log('Only download posts with property "{}".'.format(post_filter_str))
    storyitem_filter = None
    if storyitem_filter_str is not None:
        storyitem_filter = filterstr_to_filterfunc(storyitem_filter_str, StoryItem)
        instaloader.context.log('Only download storyitems with property "{}".'.format(storyitem_filter_str))
    # Login, if desired
    if username is not None:
        try:
            instaloader.load_session_from_file(username, sessionfile)
        except FileNotFoundError as err:
            if sessionfile is not None:
                print(err, file=sys.stderr)
            instaloader.context.log("Session file does not exist yet - Logging in.")
        if not instaloader.context.is_logged_in or username != instaloader.test_login():
            if password is not None:
                instaloader.login(username, password)
            else:
                instaloader.interactive_login(username)
        instaloader.context.log("Logged in as %s." % username)
    # Try block for KeyboardInterrupt (save session on ^C)
    profiles = set()
    try:
        # Generate set of profiles, already downloading non-profile targets
        for target in targetlist:
            if (target.endswith('.json') or target.endswith('.json.xz')) and os.path.isfile(target):
                with instaloader.context.error_catcher(target):
                    structure = load_structure_from_file(instaloader.context, target)
                    if isinstance(structure, Post):
                        if post_filter is not None and not post_filter(structure):
                            instaloader.context.log("<{} ({}) skipped>".format(structure, target), flush=True)
                            continue
                        instaloader.context.log("Downloading {} ({})".format(structure, target))
                        instaloader.download_post(structure, os.path.dirname(target))
                    elif isinstance(structure, StoryItem):
                        if storyitem_filter is not None and not storyitem_filter(structure):
                            instaloader.context.log("<{} ({}) skipped>".format(structure, target), flush=True)
                            continue
                        instaloader.context.log("Attempting to download {} ({})".format(structure, target))
                        instaloader.download_storyitem(structure, os.path.dirname(target))
                    elif isinstance(structure, Profile):
                        raise InvalidArgumentException("Profile JSON are ignored. Pass \"{}\" to download that profile"
                                                       .format(structure.username))
                    else:
                        raise InvalidArgumentException("{} JSON file not supported as target"
                                                       .format(structure.__class__.__name__))
                continue
            # strip '/' characters to be more shell-autocompletion-friendly
            target = target.rstrip('/')
            with instaloader.context.error_catcher(target):
                if target[0] == '@':
                    instaloader.context.log("Retrieving followees of %s..." % target[1:])
                    profile = Profile.from_username(instaloader.context, target[1:])
                    followees = profile.get_followees()
                    profiles.update([followee.username for followee in followees])
                elif target[0] == '#':
                    instaloader.download_hashtag(hashtag=target[1:], max_count=max_count, fast_update=fast_update,
                                                 post_filter=post_filter)
                elif target == ":feed":
                    instaloader.download_feed_posts(fast_update=fast_update, max_count=max_count,
                                                    post_filter=post_filter)
                elif target == ":stories":
                    instaloader.download_stories(fast_update=fast_update, storyitem_filter=storyitem_filter)
                elif target == ":saved":
                    instaloader.download_saved_posts(fast_update=fast_update, max_count=max_count,
                                                     post_filter=post_filter)
                else:
                    profiles.add(target)
        if len(profiles) > 1:
            instaloader.context.log("Downloading {} profiles: {}".format(len(profiles), ' '.join(profiles)))
        # Iterate through profiles list and download them
        for target in profiles:
            with instaloader.context.error_catcher(target):
                try:
                    instaloader.download_profile(target, profile_pic, profile_pic_only, fast_update,
                                                 stories, stories_only, post_filter=post_filter,
                                                 storyitem_filter=storyitem_filter)
                except ProfileNotExistsException as err:
                    if instaloader.context.is_logged_in:
                        instaloader.context.log(err)
                        instaloader.context.log("Trying again anonymously, helps in case you are just blocked.")
                        with instaloader.anonymous_copy() as anonymous_loader:
                            with instaloader.context.error_catcher():
                                anonymous_loader.download_profile(target, profile_pic, profile_pic_only,
                                                                  fast_update, post_filter=post_filter)
                    else:
                        raise
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
    # Save session if it is useful
    if instaloader.context.is_logged_in:
        instaloader.save_session_to_file(sessionfile)
    # User might be confused if Instaloader does nothing
    if not targetlist:
        if instaloader.context.is_logged_in:
            # Instaloader did at least save a session file
            instaloader.context.log("No targets were specified, thus nothing has been downloaded.")
        else:
            # Instloader did not do anything
            instaloader.context.log("usage:" + usage_string())


def main():
    parser = ArgumentParser(description=__doc__, add_help=False, usage=usage_string(),
                            epilog="Report issues at https://github.com/instaloader/instaloader/issues. "
                                   "The complete documentation can be found at "
                                   "https://instaloader.github.io/.")

    g_what = parser.add_argument_group('What to Download',
                                       'Specify a list of profiles or #hashtags. For each of these, Instaloader '
                                       'creates a folder and '
                                       'downloads all posts along with the pictures\'s '
                                       'captions and the current profile picture. '
                                       'If an already-downloaded profile has been renamed, Instaloader automatically '
                                       'finds it by its unique ID and renames the folder likewise.')
    g_what.add_argument('profile', nargs='*', metavar='profile|#hashtag',
                        help='Name of profile or #hashtag to download. '
                             'Alternatively, if --login is given: @<profile> to download all followees of '
                             '<profile>; the special targets '
                             ':feed to download pictures from your feed; '
                             ':stories to download the stories of your followees; or '
                             ':saved to download the posts marked as saved.')
    g_what.add_argument('-P', '--profile-pic-only', action='store_true',
                        help='Only download profile picture.')
    g_what.add_argument('--no-profile-pic', action='store_true',
                        help='Do not download profile picture.')
    g_what.add_argument('-V', '--no-videos', action='store_true',
                        help='Do not download videos.')
    g_what.add_argument('--no-video-thumbnails', action='store_true',
                        help='Do not download thumbnails of videos.')
    g_what.add_argument('-G', '--geotags', action='store_true',
                        help='Download geotags when available. Geotags are stored as a '
                             'text file with the location\'s name and a Google Maps link. '
                             'This requires an additional request to the Instagram '
                             'server for each picture, which is why it is disabled by default.')
    g_what.add_argument('-C', '--comments', action='store_true',
                        help='Download and update comments for each post. '
                             'This requires an additional request to the Instagram '
                             'server for each post, which is why it is disabled by default.')
    g_what.add_argument('--no-captions', action='store_true',
                        help='Do not create txt files.')
    g_what.add_argument('--post-metadata-txt', action='append',
                        help='Template to write in txt file for each Post.')
    g_what.add_argument('--storyitem-metadata-txt', action='append',
                        help='Template to write in txt file for each StoryItem.')
    g_what.add_argument('--no-metadata-json', action='store_true',
                        help='Do not create a JSON file containing the metadata of each post.')
    g_what.add_argument('--metadata-json', action='store_true',
                        help=SUPPRESS)
    g_what.add_argument('--no-compress-json', action='store_true',
                        help='Do not xz compress JSON files, rather create pretty formatted JSONs.')
    g_what.add_argument('-s', '--stories', action='store_true',
                        help='Also download stories of each profile that is downloaded. Requires --login.')
    g_what.add_argument('--stories-only', action='store_true',
                        help='Rather than downloading regular posts of each specified profile, only download '
                             'stories. Requires --login. Does not imply --no-profile-pic.')
    g_what.add_argument('--post-filter', '--only-if', metavar='filter',
                        help='Expression that, if given, must evaluate to True for each post to be downloaded. Must be '
                             'a syntactically valid python expression. Variables are evaluated to '
                             'instaloader.Post attributes. Example: --post-filter=viewer_has_liked.')
    g_what.add_argument('--storyitem-filter', metavar='filter',
                        help='Expression that, if given, must evaluate to True for each storyitem to be downloaded. '
                             'Must be a syntactically valid python expression. Variables are evaluated to '
                             'instaloader.StoryItem attributes.')

    g_stop = parser.add_argument_group('When to Stop Downloading',
                                       'If none of these options are given, Instaloader goes through all pictures '
                                       'matching the specified targets.')
    g_stop.add_argument('-F', '--fast-update', action='store_true',
                        help='For each target, stop when encountering the first already-downloaded picture. This '
                             'flag is recommended when you use Instaloader to update your personal Instagram archive.')
    g_stop.add_argument('-c', '--count',
                        help='Do not attempt to download more than COUNT posts. '
                             'Applies only to #hashtag and :feed.')

    g_login = parser.add_argument_group('Login (Download Private Profiles)',
                                        'Instaloader can login to Instagram. This allows downloading private profiles. '
                                        'To login, pass the --login option. Your session cookie (not your password!) '
                                        'will be saved to a local file to be reused next time you want Instaloader '
                                        'to login.')
    g_login.add_argument('-l', '--login', metavar='YOUR-USERNAME',
                         help='Login name (profile name) for your Instagram account.')
    g_login.add_argument('-f', '--sessionfile',
                         help='Path for loading and storing session key file. '
                              'Defaults to ' + get_default_session_filename("<login_name>"))
    g_login.add_argument('-p', '--password', metavar='YOUR-PASSWORD',
                         help='Password for your Instagram account. Without this option, '
                              'you\'ll be prompted for your password interactively if '
                              'there is not yet a valid session file.')

    g_how = parser.add_argument_group('How to Download')
    g_how.add_argument('--dirname-pattern',
                       help='Name of directory where to store posts. {profile} is replaced by the profile name, '
                            '{target} is replaced by the target you specified, i.e. either :feed, #hashtag or the '
                            'profile name. Defaults to \'{target}\'.')
    g_how.add_argument('--filename-pattern',
                       help='Prefix of filenames, relative to the directory given with '
                            '--dirname-pattern. {profile} is replaced by the profile name,'
                            '{target} is replaced by the target you specified, i.e. either :feed'
                            '#hashtag or the profile name. Defaults to \'{date_utc}_UTC\'')
    g_how.add_argument('--user-agent',
                       help='User Agent to use for HTTP requests. Defaults to \'{}\'.'.format(default_user_agent()))
    g_how.add_argument('-S', '--no-sleep', action='store_true', help=SUPPRESS)
    g_how.add_argument('--graphql-rate-limit', type=int, help=SUPPRESS)
    g_how.add_argument('--max-connection-attempts', metavar='N', type=int, default=3,
                       help='Maximum number of connection attempts until a request is aborted. Defaults to 3. If a '
                            'connection fails, it can be manually skipped by hitting CTRL+C. Set this to 0 to retry '
                            'infinitely.')

    g_misc = parser.add_argument_group('Miscellaneous Options')
    g_misc.add_argument('-q', '--quiet', action='store_true',
                        help='Disable user interaction, i.e. do not print messages (except errors) and fail '
                             'if login credentials are needed but not given. This makes Instaloader suitable as a '
                             'cron job.')
    g_misc.add_argument('-h', '--help', action='help', help='Show this help message and exit.')
    g_misc.add_argument('--version', action='version', help='Show version number and exit.',
                        version=__version__)

    args = parser.parse_args()
    try:
        if args.login is None and (args.stories or args.stories_only):
            print("--login=USERNAME required to download stories.", file=sys.stderr)
            args.stories = False
            if args.stories_only:
                raise SystemExit(1)

        if ':feed-all' in args.profile or ':feed-liked' in args.profile:
            raise SystemExit(":feed-all and :feed-liked were removed. Use :feed as target and "
                             "eventually --post-filter=viewer_has_liked.")

        post_metadata_txt_pattern = '\n'.join(args.post_metadata_txt) if args.post_metadata_txt else None
        storyitem_metadata_txt_pattern = '\n'.join(args.storyitem_metadata_txt) if args.storyitem_metadata_txt else None

        if args.no_captions:
            if not (post_metadata_txt_pattern or storyitem_metadata_txt_pattern):
                post_metadata_txt_pattern = ''
                storyitem_metadata_txt_pattern = ''
            else:
                raise SystemExit("--no-captions and --post-metadata-txt or --storyitem-metadata-txt given; "
                                 "That contradicts.")

        loader = Instaloader(sleep=not args.no_sleep, quiet=args.quiet, user_agent=args.user_agent,
                             dirname_pattern=args.dirname_pattern, filename_pattern=args.filename_pattern,
                             download_videos=not args.no_videos, download_video_thumbnails=not args.no_video_thumbnails,
                             download_geotags=args.geotags,
                             download_comments=args.comments, save_metadata=not args.no_metadata_json,
                             compress_json=not args.no_compress_json,
                             post_metadata_txt_pattern=post_metadata_txt_pattern,
                             storyitem_metadata_txt_pattern=storyitem_metadata_txt_pattern,
                             graphql_rate_limit=args.graphql_rate_limit,
                             max_connection_attempts=args.max_connection_attempts)
        _main(loader,
              args.profile,
              username=args.login.lower() if args.login is not None else None,
              password=args.password,
              sessionfile=args.sessionfile,
              max_count=int(args.count) if args.count is not None else None,
              profile_pic=not args.no_profile_pic,
              profile_pic_only=args.profile_pic_only,
              fast_update=args.fast_update,
              stories=args.stories,
              stories_only=args.stories_only,
              post_filter_str=args.post_filter,
              storyitem_filter_str=args.storyitem_filter)
        loader.close()
    except InstaloaderException as err:
        raise SystemExit("Fatal error: %s" % err)


if __name__ == "__main__":
    main()