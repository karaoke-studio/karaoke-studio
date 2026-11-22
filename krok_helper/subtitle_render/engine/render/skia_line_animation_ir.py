"""Check line animation modes and declare whole-line animation in Skia IR."""


def ordinary_line_animation(source):
    return (source.get('entry_anim') in {'none', 'fade', 'slide_in', 'rise'}
            and source.get('exit_anim') in {'none', 'fade', 'slide_out', 'rise'})


def is_noise_animation(source):
    animations = {source.get('entry_anim'), source.get('exit_anim')}
    return 'noise' in animations and animations <= {'none', 'noise'}


def declare_line_animations(ir):
    if any(line.get('entry_anim') == 'fade' or line.get('exit_anim') == 'fade'
           for line in ir['track']['lines']):
        ir['required_features'].append('p07_line_fade')
    if any(line.get('entry_anim') in {'slide_in', 'rise'} or line.get('exit_anim') in {'slide_out', 'rise'}
           for line in ir['track']['lines']):
        ir['required_features'].append('p07_line_motion')


def write_line_animation_schemas(ir) -> bool:
    animates_line = False
    for feature, schema in (('p07_line_fade', 'line_fade_schema'), ('p07_line_motion', 'line_motion_schema')):
        if feature in ir['required_features']:
            ir['skia_layout'][schema] = 1
            animates_line = True
    return animates_line
