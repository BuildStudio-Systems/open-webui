import {installPreview} from '../../../buildstudio-systems/portal/showcase/runtime'
import {fixture} from './fixtures'
import {installNavigation} from './navigation'
// Native relative links route only inside this isolated sample application.
installNavigation()
installPreview(fixture)
void import('./mount')
